#!/usr/bin/env bash
set -Eeuo pipefail

pve_node="${1:-}"
if [[ -z "$pve_node" ]]; then
    echo "ОШИБКА: не задан узел PVE" >&2
    exit 2
fi

status_url="http://127.0.0.1:8200/v1/sys/seal-status"
tls_dir="/etc/infra-manager/openbao/tls"
tls_ca="$tls_dir/ca.crt"
tls_certificate="$tls_dir/server.crt"
status_json=""

read_status_flags() {
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
initialized = payload.get("initialized")
sealed = payload.get("sealed")
if not isinstance(initialized, bool) or not isinstance(sealed, bool):
    raise SystemExit("Некорректное состояние OpenBao")
print(str(initialized).lower(), str(sealed).lower())
'
}

run_pve_openbao() {
    ssh \
        -i /mnt/pve-access/pve-host/root_ed25519 \
        -o UserKnownHostsFile=/mnt/pve-access/pve-host/known_hosts \
        -o StrictHostKeyChecking=yes \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        "root@${pve_node}" \
        /usr/local/sbin/infra-manager-openbao-unseal "$@"
}

for _ in $(seq 1 60); do
    if status_json="$(curl -fsS "$status_url" 2>/dev/null)"; then
        break
    fi
    sleep 2
done

if [[ -z "$status_json" ]]; then
    echo "ОШИБКА: OpenBao не стал доступен после запуска 910" >&2
    exit 1
fi

read -r initialized sealed < <(read_status_flags <<<"$status_json")

if [[ "$initialized" != "true" ]]; then
    echo "[ИНФО] OpenBao ещё не инициализирован"
    exit 0
fi

# Helper вызывается всегда: если OpenBao уже разблокирован, он всё равно
# заново материализует рабочие секреты из KV. Если запечатан — сначала
# разблокирует его, затем восстанавливает секреты.
run_pve_openbao

status_json="$(curl -fsS "$status_url")"
read -r initialized sealed < <(read_status_flags <<<"$status_json")
if [[ "$initialized" != "true" || "$sealed" != "false" ]]; then
    echo "ОШИБКА: OpenBao не достиг разблокированного состояния после запуска 910" >&2
    exit 1
fi

# Проверки выполняются через PVE-only AppRole; служебные данные OpenBao
# не передаются в 910 и не попадают в журналы.
run_pve_openbao --check-kv --log-level quiet
run_pve_openbao --check-ssh-access --log-level quiet

if [[ ! -s "$tls_ca" || ! -s "$tls_certificate" ]]; then
    echo "ОШИБКА: отсутствуют TLS-материалы OpenBao" >&2
    exit 1
fi

openbao_address="$(
    openssl x509 \
        -in "$tls_certificate" \
        -noout \
        -ext subjectAltName \
        | sed -n 's/.*IP Address:\([0-9.]\+\).*/\1/p' \
        | head -n 1
)"
if [[ -z "$openbao_address" ]]; then
    echo "ОШИБКА: TLS-сертификат OpenBao не содержит IP SAN" >&2
    exit 1
fi

tls_status_json="$(
    curl \
        -fsS \
        --connect-timeout 5 \
        --max-time 10 \
        --cacert "$tls_ca" \
        "https://${openbao_address}:8202/v1/sys/health"
)"
read -r tls_initialized tls_sealed < <(
    read_status_flags <<<"$tls_status_json"
)
if [[ "$tls_initialized" != "true" || "$tls_sealed" != "false" ]]; then
    echo "ОШИБКА: TLS-вход OpenBao :8202 не подтверждает готовое состояние" >&2
    exit 1
fi

echo "[ОК] OpenBao разблокирован, секреты восстановлены, SSH CA/OTP и TLS готовы"
