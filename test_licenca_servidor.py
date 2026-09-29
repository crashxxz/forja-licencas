import hashlib
import importlib.util
import gc
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
        self.old_admin_hash = os.environ.get("FORJA_ADMIN_PASSWORD_HASH")
        self.old_admin_totp = os.environ.get("FORJA_ADMIN_TOTP_SECRET")
        os.environ["DATABASE_URL"] = ""
        os.environ["PAYMENT_GRACE_DAYS"] = "3"
        server.DB_PATH = Path(self.temp.name) / "licenses.sqlite"
        server.ADMIN_SESSIONS.clear()
        server.ADMIN_SESSION_CSRF.clear()
        server.ADMIN_LOGIN_ATTEMPTS.clear()
        server.ADMIN_LAST_TOTP_COUNTER = -1
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
        if self.old_admin_hash is None:
            os.environ.pop("FORJA_ADMIN_PASSWORD_HASH", None)
        else:
            os.environ["FORJA_ADMIN_PASSWORD_HASH"] = self.old_admin_hash
        if self.old_admin_totp is None:
            os.environ.pop("FORJA_ADMIN_TOTP_SECRET", None)
        else:
            os.environ["FORJA_ADMIN_TOTP_SECRET"] = self.old_admin_totp
        server.ADMIN_SESSIONS.clear()
        server.ADMIN_SESSION_CSRF.clear()
        server.ADMIN_LOGIN_ATTEMPTS.clear()
        server.ADMIN_LAST_TOTP_COUNTER = -1
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

    def test_admin_password_totp_session_and_expiration(self):
        password = "Senha-Forte-Teste-2026"
        secret = "JBSWY3DPEHPK3PXP"
        fixed_time = 1800000000
        counter = fixed_time // 30
        os.environ["FORJA_ADMIN_PASSWORD_HASH"] = server.make_admin_password_hash(password, b"0123456789ABCDEF")
        os.environ["FORJA_ADMIN_TOTP_SECRET"] = secret
        code = server.admin_totp_code(counter)

        self.assertIsNone(server.create_admin_session("senha-errada", code))
        self.assertIsNone(server.create_admin_session(password, "000000"))

        original_time = server.time.time
        server.time.time = lambda: fixed_time
        try:
            token = server.create_admin_session(password, code)
            self.assertTrue(token)
            self.assertTrue(server.admin_session_valid(token))
            self.assertIsNone(server.create_admin_session(password, code))
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            server.ADMIN_SESSIONS[token_hash] = fixed_time - 1
            self.assertFalse(server.admin_session_valid(token))
        finally:
            server.time.time = original_time

    def test_admin_queries_and_activation_unlink_use_existing_schema(self):
        key = self.create_license(max_machines=2)
        machine = fingerprint("admin-machine")
        self.seed_activation(key, machine)
        self.assertEqual(len(server.admin_list_licenses("Teste")), 1)
        self.assertEqual(server.admin_get_license(key)["machines"], 1)
        updated = server.admin_update_license(key, {"expires_at": "2027-01-15", "status": "blocked"})
        self.assertEqual(updated["expires_at"], "2027-01-15")
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(len(server.admin_list_activations(key)), 1)
        self.assertTrue(server.admin_unlink_activation(key, machine))
        self.assertEqual(server.admin_list_activations(key), [])

    def test_admin_http_login_and_protected_listing(self):
        password = "Senha-Forte-HTTP-2026"
        os.environ["FORJA_ADMIN_PASSWORD_HASH"] = server.make_admin_password_hash(password, b"FEDCBA9876543210")
        os.environ["FORJA_ADMIN_TOTP_SECRET"] = "JBSWY3DPEHPK3PXP"
        self.create_license()
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.LicenseHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def post_login(password_value, code_value):
            body = json.dumps({"password": password_value, "totp": code_value}).encode("utf-8")
            request = Request(base + "/admin/login", data=body, headers={"Content-Type": "application/json"}, method="POST")
            return urlopen(request, timeout=5)

        try:
            with self.assertRaises(HTTPError) as wrong_password:
                post_login("senha-errada", server.admin_totp_code(int(server.time.time() // 30)))
            self.assertEqual(wrong_password.exception.code, 401)
            with self.assertRaises(HTTPError) as wrong_totp:
                post_login(password, "000000")
            self.assertEqual(wrong_totp.exception.code, 401)

            response = post_login(password, server.admin_totp_code(int(server.time.time() // 30)))
            token = json.loads(response.read().decode("utf-8"))["session_token"]
            request = Request(base + "/admin/licenses", headers={"Authorization": f"Bearer {token}"})
            listed = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            self.assertEqual(listed["count"], 1)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_admin_web_cookie_csrf_and_crud(self):
        password = "Senha-Forte-Web-2026"
        os.environ["FORJA_ADMIN_PASSWORD_HASH"] = server.make_admin_password_hash(password, b"ABCDEF0123456789")
        os.environ["FORJA_ADMIN_TOTP_SECRET"] = "JBSWY3DPEHPK3PXP"
        key = self.create_license(max_machines=2)
        machine = fingerprint("web-admin-machine")
        self.seed_activation(key, machine)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.LicenseHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def request_json(path, method="GET", payload=None, headers=None):
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            request_headers = {"Accept": "application/json", **(headers or {})}
            if body is not None:
                request_headers["Content-Type"] = "application/json"
            response = urlopen(Request(base + path, data=body, headers=request_headers, method=method), timeout=5)
            return response, json.loads(response.read().decode("utf-8"))

        try:
            page = urlopen(base + "/admin", timeout=5)
            html = page.read().decode("utf-8")
            self.assertIn("Administração de Licenças", html)
            self.assertNotIn(password, html)

            code = server.admin_totp_code(int(server.time.time() // 30))
            response, login = request_json("/admin/web-login", "POST", {"password": password, "totp": code})
            set_cookie = response.headers["Set-Cookie"]
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("Secure", set_cookie)
            self.assertIn("SameSite=Strict", set_cookie)
            cookie = set_cookie.split(";", 1)[0]
            csrf = login["csrf_token"]
            auth_headers = {"Cookie": cookie}
            write_headers = {"Cookie": cookie, "X-CSRF-Token": csrf}

            _, session = request_json("/admin/session", headers=auth_headers)
            self.assertEqual(session["csrf_token"], csrf)
            _, listed = request_json("/admin/licenses", headers=auth_headers)
            self.assertEqual(listed["count"], 1)

            with self.assertRaises(HTTPError) as missing_csrf:
                request_json("/admin/licenses", "POST", {
                    "customer": "Sem CSRF", "product": "igt_individual",
                    "expires_at": "2027-01-01", "max_machines": 1,
                }, auth_headers)
            self.assertEqual(missing_csrf.exception.code, 403)

            _, created = request_json("/admin/licenses", "POST", {
                "customer": "Cliente Web", "product": "jua_individual",
                "expires_at": "2027-01-01", "max_machines": 1,
            }, write_headers)
            created_key = created["license"]["license_key"]
            _, updated = request_json(f"/admin/licenses/{created_key}", "PATCH", {
                "expires_at": "2027-02-01", "status": "blocked",
            }, write_headers)
            self.assertEqual(updated["license"]["status"], "blocked")
            self.assertEqual(updated["license"]["expires_at"], "2027-02-01")

            _, activations = request_json(f"/admin/licenses/{key}/activations", headers=auth_headers)
            self.assertEqual(activations["count"], 1)
            request_json(f"/admin/licenses/{key}/activations/{machine}", "DELETE", headers=write_headers)
            self.assertEqual(self.activation_count(key), 0)

            logout_response, _ = request_json("/admin/web-logout", "POST", headers=write_headers)
            self.assertIn("Max-Age=0", logout_response.headers["Set-Cookie"])
            with self.assertRaises(HTTPError) as logged_out:
                request_json("/admin/licenses", headers=auth_headers)
            self.assertEqual(logged_out.exception.code, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


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
