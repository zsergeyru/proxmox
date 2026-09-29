#!/usr/bin/env bash
set -Eeuo pipefail

pve_node="${1:-}"
if [[ -z "$pve_node" ]]; then
    echo "ОШИБКА: не задан узел PVE" >&2
    exit 2
fi

status_url="http://127.0.0.1:8200/v1/sys/seal-status"
status_json=""

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

read -r initialized sealed < <(
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
initialized = payload.get("initialized")
sealed = payload.get("sealed")
if not isinstance(initialized, bool) or not isinstance(sealed, bool):
    raise SystemExit("Некорректное состояние OpenBao")
print(str(initialized).lower(), str(sealed).lower())
' <<<"$status_json"
)

if [[ "$initialized" != "true" ]]; then
    echo "[ИНФО] OpenBao ещё не инициализирован"
    exit 0
fi

if [[ "$sealed" != "true" ]]; then
    echo "[ОК] OpenBao уже разблокирован"
    exit 0
fi

ssh \
    -i /mnt/pve-access/pve-host/root_ed25519 \
    -o UserKnownHostsFile=/mnt/pve-access/pve-host/known_hosts \
    -o StrictHostKeyChecking=yes \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    "root@${pve_node}" \
    /usr/local/sbin/infra-manager-openbao-unseal

status_json="$(curl -fsS "$status_url")"
read -r initialized sealed < <(
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
print(
    str(payload.get("initialized")).lower(),
    str(payload.get("sealed")).lower(),
)
' <<<"$status_json"
)

if [[ "$initialized" == "true" && "$sealed" == "false" ]]; then
    echo "[ОК] OpenBao разблокирован после запуска 910"
    exit 0
fi

echo "ОШИБКА: OpenBao остался запечатан после запуска 910" >&2
exit 1
