import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "CHAVES_GERADAS.txt"
PYTHON = sys.executable

PRODUCTS = {
    "1": ("igt_individual", "Iguatu Individual"),
    "2": ("jua_individual", "Juazeiro Individual"),
    "3": ("jua_emp", "Juazeiro Empreendimento"),
}


def pause():
    input("\nEnter para continuar...")


def run_server_cmd(args):
    result = subprocess.run(
        [PYTHON, "licenca_servidor.py", *args],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def save_record(text):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {text}\n")


def copy_key(key):
    if not key:
        return
    try:
        subprocess.run("clip", input=key, text=True, shell=True, check=False)
        print(f"\nChave copiada: {key}")
    except Exception:
        print(f"\nChave: {key}")


def choose_product():
    print("\nProduto:")
    for number, (_product, label) in PRODUCTS.items():
        print(f"{number} - {label}")
    choice = input("Escolher: ").strip()
    if choice not in PRODUCTS:
        print("Produto invalido.")
        return None, None
    return PRODUCTS[choice]


def ask_customer():
    customer = input("Nome do cliente: ").strip()
    if not customer:
        print("Cliente vazio.")
        return None
    return customer


def ask_key():
    key = input("Colar chave DOCFLOW: ").strip().upper()
    if not key:
        print("Chave vazia.")
        return None
    return key


def ask_days(default_days):
    raw = input(f"Dias para adicionar [{default_days}]: ").strip()
    if not raw:
        return default_days
    try:
        days = int(raw)
    except ValueError:
        print("Numero invalido.")
        return None
    if days <= 0:
        print("Dias precisa ser maior que zero.")
        return None
    return days


def create_license(days, title):
    product, label = choose_product()
    if not product:
        return
    customer = ask_customer()
    if not customer:
        return
    output = run_server_cmd([
        "create",
        "--customer", customer,
        "--product", product,
        "--months", "0",
        "--days", str(days),
        "--max-machines", "1",
        "--notes", title,
    ])
    try:
        data = json.loads(output)
    except Exception:
        return
    key = data.get("license_key", "")
    expires = data.get("expires_at", "")
    save_record(f"CRIADA | {title} | {label} | {customer} | {key} | vence {expires}")
    copy_key(key)
    print(f"Validade: {expires}")


def add_time():
    key = ask_key()
    if not key:
        return
    days = ask_days(30)
    if not days:
        return
    output = run_server_cmd(["renew", "--key", key, "--months", "0", "--days", str(days)])
    save_record(f"TEMPO ADICIONADO | +{days} dias | {output}")
    print("\nTempo adicionado na mesma chave.")
    print("Cliente nao precisa receber chave nova.")


def set_status(status, label):
    key = ask_key()
    if not key:
        return
    output = run_server_cmd([status, "--key", key])
    save_record(f"{label.upper()} | {output}")


def list_keys():
    run_server_cmd(["list"])


def show_help():
    print("\nComo pensar:")
    print("Chave = codigo que o cliente cola no app.")
    print("Adicionar tempo = colocar credito na mesma chave.")
    print("Teste 30 dias = criar chave nova com validade de 30 dias.")
    print("Mensal 30 dias = criar chave nova para cliente pagante.")
    print("Renovar = adicionar mais dias na chave que ja existe.")
    print("A tolerancia apos vencer continua sendo 3 dias no servidor.")


def menu():
    while True:
        print("\n==============================")
        print(" GERENCIAR LICENCAS FORJA")
        print("==============================")
        print("1 - Criar chave teste 30 dias")
        print("2 - Criar chave teste 3 dias")
        print("3 - Criar chave mensal 30 dias")
        print("4 - Adicionar tempo em chave existente")
        print("5 - Listar chaves")
        print("6 - Bloquear chave")
        print("7 - Liberar chave")
        print("8 - Explicacao rapida")
        print("0 - Sair")
        choice = input("Escolher: ").strip()
        try:
            if choice == "1":
                create_license(30, "teste 30 dias")
                pause()
            elif choice == "2":
                create_license(3, "teste 3 dias")
                pause()
            elif choice == "3":
                create_license(30, "mensal 30 dias")
                pause()
            elif choice == "4":
                add_time()
                pause()
            elif choice == "5":
                list_keys()
                pause()
            elif choice == "6":
                set_status("block", "bloqueada")
                pause()
            elif choice == "7":
                set_status("unblock", "liberada")
                pause()
            elif choice == "8":
                show_help()
                pause()
            elif choice == "0":
                break
            else:
                print("Opcao invalida.")
        except Exception as exc:
            print(f"Erro: {exc}")
            pause()


if __name__ == "__main__":
    menu()
