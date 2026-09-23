#!/usr/bin/env bash
set -Eeuo pipefail

CA_BUNDLE="/etc/infra-manager/ca/ca-bundle.crt"
NODE="${PACKER_NODE:-pve}"
TEST_VMID=9099
TEST_NAME="smoke-template-9000"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }

usage() {
    cat <<'USAGE'
Использование:
  test-template.sh 9000

Создаёт временный Full Clone 9099, проверяет Cloud-Init, QGA, SSH,
machine-id и SSH host keys, затем удаляет клон.
При ошибке 9099 сохраняется для диагностики.
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }
[[ $# -eq 1 && "$1" == "9000" ]] || { usage >&2; exit 2; }

for command in curl jq ssh ssh-keygen; do
    command -v "$command" >/dev/null 2>&1 || die "Не найден $command"
done
[[ -s "$CA_BUNDLE" ]] || die "Не найден CA bundle: $CA_BUNDLE"

: "${TF_VAR_pve_endpoint:?TF_VAR_pve_endpoint не задан}"
: "${TF_VAR_pve_api_token:?TF_VAR_pve_api_token не задан}"

PVE_API="${TF_VAR_pve_endpoint%/}/api2/json"
PVE_TOKEN_ID="${TF_VAR_pve_api_token%%=*}"
PVE_TOKEN_SECRET="${TF_VAR_pve_api_token#*=}"
AUTH_HEADER="Authorization: PVEAPIToken=${PVE_TOKEN_ID}=${PVE_TOKEN_SECRET}"

tmp="$(mktemp -d)"
key="$tmp/id_ed25519"
known_hosts="$tmp/known_hosts"
cleanup_local() {
    rm -rf "$tmp"
    unset PVE_TOKEN_SECRET AUTH_HEADER
}
trap cleanup_local EXIT

api() {
    local method=$1 endpoint=$2
    shift 2

    local response_file http_code
    response_file="$(mktemp)"
    http_code="$(curl -sS \
        --connect-timeout 5 \
        --max-time 120 \
        --cacert "$CA_BUNDLE" \
        -H "$AUTH_HEADER" \
        -X "$method" \
        -o "$response_file" \
        -w '%{http_code}' \
        "$@" \
        "${PVE_API}${endpoint}")" || {
        rm -f "$response_file"
        printf 'ОШИБКА: PVE API %s %s: ошибка соединения\n' "$method" "$endpoint" >&2
        return 1
    }

    if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
        printf 'ОШИБКА: PVE API %s %s вернул HTTP %s\n' "$method" "$endpoint" "$http_code" >&2
        if jq -e . "$response_file" >/dev/null 2>&1; then
            jq -c . "$response_file" >&2
        else
            cat "$response_file" >&2
            printf '\n' >&2
        fi
        rm -f "$response_file"
        return 1
    fi

    cat "$response_file"
    rm -f "$response_file"
}

wait_task() {
    local upid=$1 status exitstatus response
    for _ in $(seq 1 180); do
        response="$(api GET "/nodes/${NODE}/tasks/${upid}/status")"
        status="$(jq -r '.data.status // empty' <<<"$response")"
        if [[ "$status" == "stopped" ]]; then
            exitstatus="$(jq -r '.data.exitstatus // empty' <<<"$response")"
            [[ "$exitstatus" == "OK" ]] || die "Задача PVE завершилась: ${exitstatus:-неизвестно}"
            return
        fi
        sleep 1
    done
    die "Задача PVE не завершилась: $upid"
}

run_task() {
    local method=$1 endpoint=$2 response upid
    shift 2
    response="$(api "$method" "$endpoint" "$@")"
    upid="$(jq -r '.data // empty' <<<"$response")"
    [[ -n "$upid" && "$upid" != "null" ]] || die "PVE не вернул task id"
    wait_task "$upid"
}

test_vmid_free() {
    ! api GET "/cluster/resources" --get --data-urlencode "type=vm" \
        | jq -e --argjson vmid "$TEST_VMID" '.data[]? | select(.vmid == $vmid)' >/dev/null
}

wait_ipv4() {
    local response ip
    for _ in $(seq 1 120); do
        response="$(api GET "/nodes/${NODE}/qemu/${TEST_VMID}/agent/network-get-interfaces" 2>/dev/null || true)"
        ip="$(jq -r '
            .data.result[]?.["ip-addresses"][]?
            | select(.["ip-address-type"] == "ipv4")
            | .["ip-address"]
            | select(. != "127.0.0.1")
        ' <<<"$response" 2>/dev/null | head -n1)"
        if [[ -n "$ip" ]]; then
            printf '%s' "$ip"
            return 0
        fi
        sleep 2
    done
    return 1
}

delete_test_vm() {
    local state
    state="$(api GET "/nodes/${NODE}/qemu/${TEST_VMID}/status/current" | jq -r '.data.status // empty' || true)"
    if [[ "$state" == "running" ]]; then
        run_task POST "/nodes/${NODE}/qemu/${TEST_VMID}/status/stop"
    fi
    run_task DELETE "/nodes/${NODE}/qemu/${TEST_VMID}" --data-urlencode "purge=1"
}

test_vmid_free || die "VMID $TEST_VMID уже занят; ничего не изменено"
ssh-keygen -q -t ed25519 -N '' -f "$key"

info "Создание проверочного Full Clone $TEST_VMID"
run_task POST "/nodes/${NODE}/qemu/9000/clone" \
    --data-urlencode "newid=$TEST_VMID" \
    --data-urlencode "name=$TEST_NAME" \
    --data-urlencode "full=1" \
    --data-urlencode "storage=local-lvm"
api PUT "/nodes/${NODE}/qemu/${TEST_VMID}/config" \
    --data-urlencode "cores=1" \
    --data-urlencode "memory=768" \
    --data-urlencode "protection=0" \
    --data-urlencode "ciuser=root" \
    --data-urlencode "ipconfig0=ip=dhcp" \
    --data-urlencode "sshkeys=$(cat "$key.pub")" >/dev/null

run_task POST "/nodes/${NODE}/qemu/${TEST_VMID}/status/start"

ip="$(wait_ipv4)" || die "QEMU Guest Agent не сообщил IPv4; VM $TEST_VMID оставлена для диагностики"
info "Проверочная VM получила $ip"

for _ in $(seq 1 60); do
    if ssh \
        -i "$key" \
        -o BatchMode=yes \
        -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$known_hosts" \
        "root@$ip" true >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

ssh \
    -i "$key" \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$known_hosts" \
    "root@$ip" \
    'cloud-init status --wait >/dev/null &&
     systemctl is-active --quiet qemu-guest-agent &&
     test -s /etc/machine-id &&
     compgen -G "/etc/ssh/ssh_host_*_key" >/dev/null' \
    || die "Проверка внутри клона не пройдена; VM $TEST_VMID оставлена для диагностики"

delete_test_vm
ok "Full Clone шаблона 9000 успешно проверен"
