#!/usr/bin/env bash
set -Eeuo pipefail

PVE_ENV="/etc/infra-deployer/secrets/pve-api.env"
CA_BUNDLE="/etc/infra-deployer/ca/ca-bundle.crt"
TEST_VMID=9098
TEST_HOSTNAME="infra-access-test"
MANAGED_POOL="managed"
ROOT_STORAGE="local-lvm"
TEMPLATE_STORAGE="local"
BRIDGE="vmbr0"

APPLY=0
CREATED=0
NODE=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }
warn() { printf '[ПРЕДУПРЕЖДЕНИЕ] %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
Использование:
  infra-deployer-pve-lifecycle-test --apply

Явный интеграционный тест PVE API.
Создаёт временный LXC 9098 в pool managed, изменяет его, запускает,
останавливает и удаляет. Без --apply никаких изменений не выполняется.
USAGE
}

if [[ "${1:-}" == "--apply" && $# -eq 1 ]]; then
    APPLY=1
elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
else
    usage >&2
    die "Для запуска теста требуется единственный параметр --apply"
fi

((APPLY == 1)) || die "Тест не подтверждён"
[[ $EUID -eq 0 ]] || die "Запустите тест от root внутри 910"
[[ -s "$PVE_ENV" ]] || die "Не найден $PVE_ENV"
[[ -s "$CA_BUNDLE" ]] || die "Не найден $CA_BUNDLE"

read_env_value() {
    local key=$1
    sed -n "s/^${key}=//p" "$PVE_ENV" | head -n1
}

PVE_API_URL="$(read_env_value PVE_API_URL)"
PVE_API_TOKEN_ID="$(read_env_value PVE_API_TOKEN_ID)"
PVE_API_TOKEN_SECRET="$(read_env_value PVE_API_TOKEN_SECRET)"
[[ -n "$PVE_API_URL" && -n "$PVE_API_TOKEN_ID" && -n "$PVE_API_TOKEN_SECRET" ]] \
    || die "PVE credential неполон"
AUTH_HEADER="Authorization: PVEAPIToken=${PVE_API_TOKEN_ID}=${PVE_API_TOKEN_SECRET}"

api() {
    local method=$1 endpoint=$2
    shift 2
    curl -fsS \
        --connect-timeout 5 \
        --max-time 60 \
        --cacert "$CA_BUNDLE" \
        -H "$AUTH_HEADER" \
        -X "$method" \
        "$@" \
        "${PVE_API_URL}/api2/json${endpoint}"
}

wait_task() {
    local upid=$1 response status exitstatus _
    for _ in $(seq 1 120); do
        response="$(api GET "/nodes/${NODE}/tasks/${upid}/status")" \
            || die "Не удалось прочитать status task $upid"
        status="$(jq -r '.data.status // empty' <<<"$response")"
        if [[ "$status" == "stopped" ]]; then
            exitstatus="$(jq -r '.data.exitstatus // empty' <<<"$response")"
            [[ "$exitstatus" == "OK" ]] || die "PVE task завершился: ${exitstatus:-неизвестно}"
            return
        fi
        sleep 1
    done
    die "PVE task не завершился за отведённое время: $upid"
}

run_task() {
    local method=$1 endpoint=$2 response upid
    shift 2
    response="$(api "$method" "$endpoint" "$@")"
    upid="$(jq -r '.data // empty' <<<"$response")"
    [[ -n "$upid" && "$upid" != "null" ]] || die "PVE не вернул task id для $method $endpoint"
    wait_task "$upid"
}

vmid_exists() {
    api GET "/cluster/resources" --get --data-urlencode "type=vm" \
        | jq -e --argjson vmid "$TEST_VMID" '.data[]? | select(.vmid == $vmid)' >/dev/null
}

cleanup() {
    local response upid
    ((CREATED == 1)) || return 0
    warn "Тест прерван; выполняется попытка удалить временный LXC $TEST_VMID"

    response="$(api POST "/nodes/${NODE}/lxc/${TEST_VMID}/status/stop" 2>/dev/null || true)"
    upid="$(jq -r '.data // empty' <<<"$response" 2>/dev/null || true)"
    if [[ -n "$upid" && "$upid" != "null" ]]; then
        wait_task "$upid" >/dev/null 2>&1 || true
    fi

    response="$(api DELETE "/nodes/${NODE}/lxc/${TEST_VMID}" 2>/dev/null || true)"
    upid="$(jq -r '.data // empty' <<<"$response" 2>/dev/null || true)"
    if [[ -n "$upid" && "$upid" != "null" ]]; then
        wait_task "$upid" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

NODE="$(api GET "/nodes" | jq -r '.data[0].node // empty')"
[[ -n "$NODE" ]] || die "Не удалось определить PVE node"

vmid_exists && die "VMID $TEST_VMID уже занят; тест ничего не удаляет"

TEMPLATE_VOLID="$(api GET "/nodes/${NODE}/storage/${TEMPLATE_STORAGE}/content" \
    --get --data-urlencode "content=vztmpl" \
    | jq -r '.data[]?.volid | select(test("vztmpl/debian-13-standard_.*_amd64\\.tar\\.(zst|gz)$"))' \
    | sort -V | tail -n1)"
[[ -n "$TEMPLATE_VOLID" ]] || die "Не найден Debian 13 LXC template на $TEMPLATE_STORAGE"

printf "Будет создан временный LXC %s на node %s из %s\n" "$TEST_VMID" "$NODE" "$TEMPLATE_VOLID"

response="$(api POST "/nodes/${NODE}/lxc" \
    --data-urlencode "vmid=$TEST_VMID" \
    --data-urlencode "hostname=$TEST_HOSTNAME" \
    --data-urlencode "pool=$MANAGED_POOL" \
    --data-urlencode "ostemplate=$TEMPLATE_VOLID" \
    --data-urlencode "ostype=debian" \
    --data-urlencode "unprivileged=1" \
    --data-urlencode "cores=1" \
    --data-urlencode "memory=256" \
    --data-urlencode "swap=0" \
    --data-urlencode "rootfs=${ROOT_STORAGE}:2" \
    --data-urlencode "features=nesting=1" \
    --data-urlencode "net0=name=eth0,bridge=${BRIDGE},ip=dhcp,type=veth" \
    --data-urlencode "onboot=0")"
upid="$(jq -r '.data // empty' <<<"$response")"
[[ -n "$upid" && "$upid" != "null" ]] || die "PVE не вернул task id создания LXC"
CREATED=1
wait_task "$upid"
ok "LXC $TEST_VMID создан сразу в $MANAGED_POOL"

api GET "/pools/$MANAGED_POOL" \
    | jq -e --argjson vmid "$TEST_VMID" '.data.members[]? | select(.vmid == $vmid)' >/dev/null \
    || die "Созданный LXC не найден в pool $MANAGED_POOL"

api PUT "/nodes/${NODE}/lxc/${TEST_VMID}/config" \
    --data-urlencode "memory=384" >/dev/null
actual_memory="$(api GET "/nodes/${NODE}/lxc/${TEST_VMID}/config" | jq -r '.data.memory // empty')"
[[ "$actual_memory" == "384" ]] || die "Изменение RAM не применилось"
ok "Конфигурация LXC изменена"

run_task POST "/nodes/${NODE}/lxc/${TEST_VMID}/status/start"
status="$(api GET "/nodes/${NODE}/lxc/${TEST_VMID}/status/current" | jq -r '.data.status // empty')"
[[ "$status" == "running" ]] || die "LXC не запустился"
ok "LXC запущен"

run_task POST "/nodes/${NODE}/lxc/${TEST_VMID}/status/stop"
status="$(api GET "/nodes/${NODE}/lxc/${TEST_VMID}/status/current" | jq -r '.data.status // empty')"
[[ "$status" == "stopped" ]] || die "LXC не остановился"
ok "LXC остановлен"

run_task DELETE "/nodes/${NODE}/lxc/${TEST_VMID}"
CREATED=0

vmid_exists && die "После удаления VMID $TEST_VMID всё ещё существует"
ok "Временный LXC удалён"

unset PVE_API_TOKEN_SECRET AUTH_HEADER
printf "\nИнтеграционный тест PVE API успешно завершён.\n"
