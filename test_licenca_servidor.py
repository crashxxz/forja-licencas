import hashlib
import importlib.util
import gc
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

import licenca_servidor as server


ROOT = Path(__file__).resolve().parent.parent
CLIENTS = (
    ROOT / "app-igt-individual" / "app" / "license_core.py",
    ROOT / "app-jua-individual" / "app" / "license_core.py",
    ROOT / "app-jua-emp" / "license_core.py",
)


def fingerprint(label):
    return hashlib.sha256(label.encode("ascii")).hexdigest().upper()[:32]


class LicenseServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = server.DB_PATH
        self.old_database_url = os.environ.get("DATABASE_URL")
        self.old_grace = os.environ.get("PAYMENT_GRACE_DAYS")
        os.environ["DATABASE_URL"] = ""
        os.environ["PAYMENT_GRACE_DAYS"] = "3"
        server.DB_PATH = Path(self.temp.name) / "licenses.sqlite"
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self.old_db_path
        if self.old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.old_database_url
        if self.old_grace is None:
            os.environ.pop("PAYMENT_GRACE_DAYS", None)
        else:
            os.environ["PAYMENT_GRACE_DAYS"] = self.old_grace
        gc.collect()
        self.temp.cleanup()

    def create_license(self, key="DOCFLOW-0001-0002-0003-0004", max_machines=1, expires=None):
        expires = expires or server.today() + timedelta(days=30)
        with server.connect() as conn:
            conn.execute(
                """
                INSERT INTO licenses
                (license_key, product, customer, status, expires_at, max_machines, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, "igt_individual", "Teste", "active", expires.isoformat(), max_machines, "", "2026-01-01T00:00:00"),
            )
        return key

    def payload(self, key, machine, **extra):
        return {"license_key": key, "machine_id": machine, "product": "igt_individual", **extra}

    def activation_count(self, key):
        with server.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS total FROM activations WHERE license_key = ?", (key,)).fetchone()["total"]

    def seed_activation(self, key, machine):
        with server.connect() as conn:
            conn.execute(
                "INSERT INTO activations (license_key, machine_id, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (key, machine, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )

    def test_same_machine_reuses_record_and_full_limit_allows_existing(self):
        key = self.create_license(max_machines=1)
        machine = fingerprint("same")
        first = server.license_response(self.payload(key, machine), activate=True)
        second = server.license_response(self.payload(key, machine), activate=True)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.activation_count(key), 1)

    def test_full_limit_blocks_new_machine(self):
        key = self.create_license(max_machines=1)
        self.assertTrue(server.license_response(self.payload(key, fingerprint("one")), activate=True)["ok"])
        response = server.license_response(self.payload(key, fingerprint("two")), activate=True)
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "machine_limit")
        self.assertEqual(self.activation_count(key), 1)

    def test_copied_legacy_id_cannot_migrate_without_matching_proof(self):
        key = self.create_license(max_machines=1)
        stable = "SOURCE-MACHINE-GUID"
        hostname = "SOURCE-PC"
        node = "187723572702975"
        legacy = hashlib.sha256(f"{stable}|{hostname}|{node}".encode()).hexdigest().upper()[:32]
        self.seed_activation(key, legacy)

        attacker_stable = "OTHER-MACHINE-GUID"
        attacker = server._fingerprint_v2(attacker_stable)
        copied = self.payload(
            key,
            attacker,
            legacy_machine_ids=[legacy],
            migration_proof={
                "source": "machine_guid",
                "stable_value": attacker_stable,
                "hostname": "OTHER-PC",
                "node": "187723572700001",
                "legacy_machine_id": legacy,
            },
        )
        response = server.license_response(copied, activate=True)
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "machine_limit")
        self.assertEqual(self.activation_count(key), 1)

    def test_valid_legacy_migration_adds_alias_without_replacing_record(self):
        key = self.create_license(max_machines=1)
        identity = {
            "source": "machine_guid",
            "stable_value": "LEGIT-MACHINE-GUID",
            "hostname": "LEGIT-PC",
            "node": "187723572702975",
        }
        legacy = hashlib.sha256(
            f"{identity['stable_value']}|{identity['hostname']}|{identity['node']}".encode()
        ).hexdigest().upper()[:32]
        current = server._fingerprint_v2(identity["stable_value"])
        self.seed_activation(key, legacy)
        proof = dict(identity, legacy_machine_id=legacy)

        response = server.license_response(self.payload(key, current, migration_proof=proof), activate=True)
        self.assertTrue(response["ok"])
        self.assertEqual(self.activation_count(key), 1)
        with server.connect() as conn:
            old_exists = conn.execute(
                "SELECT 1 FROM activations WHERE license_key = ? AND machine_id = ?", (key, legacy)
            ).fetchone()
            alias = conn.execute(
                "SELECT activation_machine_id FROM activation_aliases WHERE license_key = ? AND machine_id = ?",
                (key, current),
            ).fetchone()
        self.assertIsNotNone(old_exists)
        self.assertEqual(alias["activation_machine_id"], legacy)

    def test_expiration_grace_requires_authorized_machine(self):
        key = self.create_license(expires=server.today() - timedelta(days=1))
        authorized = fingerprint("authorized")
        self.seed_activation(key, authorized)

        allowed = server.license_response(self.payload(key, authorized), activate=False)
        denied = server.license_response(self.payload(key, fingerprint("unauthorized")), activate=True)
        self.assertTrue(allowed["ok"])
        self.assertIn("Tolerancia", allowed["message"])
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["status"], "not_activated")
        self.assertEqual(self.activation_count(key), 1)

    def run_concurrently(self, calls):
        barrier = threading.Barrier(len(calls))
        results = []
        errors = []

        def worker(call):
            try:
                barrier.wait(timeout=5)
                results.append(call())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(call,)) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return results

    def test_concurrent_same_machine_does_not_duplicate(self):
        key = self.create_license(max_machines=1)
        machine = fingerprint("concurrent-same")
        results = self.run_concurrently([
            lambda: server.license_response(self.payload(key, machine), activate=True),
            lambda: server.license_response(self.payload(key, machine), activate=True),
        ])
        self.assertEqual(sum(bool(item["ok"]) for item in results), 2)
        self.assertEqual(self.activation_count(key), 1)

    def test_concurrent_last_slot_never_exceeds_limit(self):
        key = self.create_license(max_machines=1)
        machines = [fingerprint("race-one"), fingerprint("race-two")]
        results = self.run_concurrently([
            lambda machine=machine: server.license_response(self.payload(key, machine), activate=True)
            for machine in machines
        ])
        self.assertEqual(sum(bool(item["ok"]) for item in results), 1)
        self.assertEqual(sum(item["status"] == "machine_limit" for item in results), 1)
        self.assertEqual(self.activation_count(key), 1)


class ClientFingerprintTests(unittest.TestCase):
    def test_all_clients_ignore_folder_hostname_user_and_ip_for_primary_fingerprint(self):
        base = {
            "source": "machine_guid",
            "stable_value": "12345678-1234-1234-1234-123456789ABC",
            "hostname": "PC-ONE",
            "node": "187723572702975",
        }
        changed = dict(base, hostname="PC-TWO", node="187723572700001")
        for index, path in enumerate(CLIENTS):
            spec = importlib.util.spec_from_file_location(f"license_core_test_{index}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.machine_id_from_identity(base), module.machine_id_from_identity(changed))
            self.assertEqual(len(module.machine_id_from_identity(base)), 32)


if __name__ == "__main__":
    unittest.main()
