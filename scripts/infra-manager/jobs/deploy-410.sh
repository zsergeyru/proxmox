#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
OPENTOFU_DIR="$REPO_ROOT/automation/opentofu"
STATE_DIR="/var/lib/infra-manager/opentofu"
GUEST_STATE_FILE="$STATE_DIR/guests.json"
CA_BUNDLE="/etc/infra-manager/ca/ca-bundle.crt"
PLAN_FILE="$STATE_DIR/410.tfplan"
ANSIBLE_PRIVATE_KEY="/etc/infra-manager/ansible/guest_ed25519"
KNOWN_HOSTS="/var/lib/semaphore/guest-known-hosts"
PLAYBOOK="$REPO_ROOT/automation/ansible/playbooks/configure-guest.yml"
GUEST_DIR="410-ai-control"
TARGET_RESOURCE='proxmox_virtual_environment_vm.guest["410"]'
TEMPLATE_VMID=9000
TEMPLATE_PROTECTION_CHANGED=0

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }

cleanup() {
    local rc=$?

    rm -f "$PLAN_FILE"

    if ((TEMPLATE_PROTECTION_CHANGED == 1)) && declare -F api >/dev/null 2>&1; then
        if api PUT "/nodes/pve/qemu/${TEMPLATE_VMID}/config" \
            --data-urlencode "protection=1" >/dev/null; then
            printf '[ОК] Защита шаблона %s восстановлена\n' "$TEMPLATE_VMID"
        else
            printf 'ОШИБКА: не удалось восстановить protection шаблона %s\n' "$TEMPLATE_VMID" >&2
            rc=1
        fi
        TEMPLATE_PROTECTION_CHANGED=0
    fi

    unset PVE_TOKEN_SECRET AUTH_HEADER
    trap - EXIT
    exit "$rc"
}
trap cleanup EXIT

for command in python3 jq curl tofu ansible-playbook ssh-keygen ssh-keyscan; do
    command -v "$command" >/dev/null 2>&1 || die "Не найден $command"
done

[[ -n "${TF_VAR_pve_endpoint:-}" ]] || die "Не задан TF_VAR_pve_endpoint"
[[ -n "${TF_VAR_pve_api_token:-}" ]] || die "Не задан TF_VAR_pve_api_token"
[[ -n "${TF_VAR_ansible_ssh_public_key:-}" ]] || die "Не задан TF_VAR_ansible_ssh_public_key"
[[ -r "$ANSIBLE_PRIVATE_KEY" ]] || die "Не найден закрытый ключ Ansible"
[[ -f "$PLAYBOOK" ]] || die "Не найден playbook $PLAYBOOK"
[[ -s "$CA_BUNDLE" ]] || die "Не найден CA bundle: $CA_BUNDLE"

install -d -m 0750 "$STATE_DIR"
touch "$KNOWN_HOSTS"
chmod 0600 "$KNOWN_HOSTS"
umask 077

# OpenTofu provider использует системный TLS стек Go.
# Передаём ему подготовленный bundle с PVE CA, не отключая проверку сертификата.
export SSL_CERT_FILE="$CA_BUNDLE"

info "Проверка проекта"
python3 "$REPO_ROOT/scripts/validate_repo.py"

info "Подготовка итогового состояния гостей"
python3 "$REPO_ROOT/scripts/guests/render-opentofu-input.py" --output "$GUEST_STATE_FILE"

guest_type="$(jq -r '.guests["410"].type // empty' "$GUEST_STATE_FILE")"
guest_name="$(jq -r '.guests["410"].name // empty' "$GUEST_STATE_FILE")"
guest_ip="$(jq -r '.guests["410"].network.ipv4 // empty' "$GUEST_STATE_FILE")"

[[ "$guest_type" == "vm" ]] || die "VMID 410 должен иметь type=vm"
[[ "$guest_name" == "ai-control" ]] || die "VMID 410 должен иметь name=ai-control"
[[ -n "$guest_ip" && "$guest_ip" != "dhcp" ]] || die "Для первого запуска 410 ожидается статический IPv4"

guest_ip="${guest_ip%%/*}"

PVE_ENDPOINT="${TF_VAR_pve_endpoint%/}"
PVE_API="${PVE_ENDPOINT}/api2/json"
PVE_TOKEN_ID="${TF_VAR_pve_api_token%%=*}"
PVE_TOKEN_SECRET="${TF_VAR_pve_api_token#*=}"
AUTH_HEADER="Authorization: PVEAPIToken=${PVE_TOKEN_ID}=${PVE_TOKEN_SECRET}"

[[ -n "$PVE_TOKEN_ID" && -n "$PVE_TOKEN_SECRET" && "$PVE_TOKEN_ID" != "$PVE_TOKEN_SECRET" ]] \
    || die "TF_VAR_pve_api_token имеет неверный формат"

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
        response="$(api GET "/nodes/pve/tasks/${upid}/status")"
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

prepare_template_for_clone() {
    local response template name protection

    response="$(api GET "/nodes/pve/qemu/${TEMPLATE_VMID}/config")" \
        || die "Не удалось прочитать шаблон $TEMPLATE_VMID"

    template="$(jq -r '.data.template // 0' <<<"$response")"
    name="$(jq -r '.data.name // empty' <<<"$response")"
    protection="$(jq -r '.data.protection // 0' <<<"$response")"

    [[ "$template" == "1" || "$template" == "true" ]] \
        || die "VMID $TEMPLATE_VMID не является шаблоном"
    [[ "$name" == "tpl-debian13" ]] \
        || die "VMID $TEMPLATE_VMID имеет неожиданное имя '$name'"

    if [[ "$protection" == "1" || "$protection" == "true" ]]; then
        info "Временное снятие защиты шаблона $TEMPLATE_VMID для клонирования"
        api PUT "/nodes/pve/qemu/${TEMPLATE_VMID}/config" \
            --data-urlencode "protection=0" >/dev/null
        TEMPLATE_PROTECTION_CHANGED=1
    fi
}

restore_template_protection() {
    if ((TEMPLATE_PROTECTION_CHANGED == 1)); then
        api PUT "/nodes/pve/qemu/${TEMPLATE_VMID}/config" \
            --data-urlencode "protection=1" >/dev/null \
            || die "Не удалось восстановить protection шаблона $TEMPLATE_VMID"
        TEMPLATE_PROTECTION_CHANGED=0
        ok "Защита шаблона $TEMPLATE_VMID восстановлена"
    fi
}

pve_guest_410() {
    curl -fsS \
        --connect-timeout 5 \
        --max-time 30 \
        --cacert "$CA_BUNDLE" \
        -H "$AUTH_HEADER" \
        --get \
        --data-urlencode "type=vm" \
        "${PVE_API}/cluster/resources" \
      | jq -c '.data[]? | select((.vmid | tostring) == "410")' \
      | head -n1
}

info "Инициализация OpenTofu"
tofu -chdir="$OPENTOFU_DIR" init \
    -input=false \
    -no-color \
    -lockfile=readonly

state_present=0
state_status=""
if tofu -chdir="$OPENTOFU_DIR" state show "$TARGET_RESOURCE" >/dev/null 2>&1; then
    state_present=1
    state_status="$(
        tofu -chdir="$OPENTOFU_DIR" state pull \
          | jq -r '
              .resources[]
              | select(.type == "proxmox_virtual_environment_vm" and .name == "guest")
              | .instances[]
              | select((.index_key | tostring) == "410")
              | .status // "ready"
            ' \
          | head -n1
    )"
fi

pve_guest="$(pve_guest_410)" || die "Не удалось проверить VMID 410 через PVE API"

if [[ -n "$pve_guest" ]]; then
    pve_type="$(jq -r '.type // empty' <<<"$pve_guest")"
    pve_name="$(jq -r '.name // empty' <<<"$pve_guest")"

    [[ "$pve_type" == "qemu" && "$pve_name" == "ai-control" ]] || {
        die "VMID 410 уже занят объектом '$pve_name' типа '$pve_type'; автоматическое управление запрещено"
    }

    if ((state_present == 0)); then
        die "VM 410 существует в PVE, но отсутствует в OpenTofu state; автоматический импорт клонированной VM запрещён"
    fi

    if [[ "$state_status" == "tainted" ]]; then
        info "Удаление незавершённой VM 410 после неудачного первого применения"

        pve_state="$(api GET "/nodes/pve/qemu/410/status/current" | jq -r '.data.status // empty')"
        if [[ "$pve_state" == "running" ]]; then
            run_task POST "/nodes/pve/qemu/410/status/stop"
        fi

        api PUT "/nodes/pve/qemu/410/config" \
            --data-urlencode "protection=0" >/dev/null

        run_task DELETE "/nodes/pve/qemu/410" \
            --get \
            --data-urlencode "purge=1"

        pve_guest="$(pve_guest_410)" || die "Не удалось проверить удаление VMID 410"
        [[ -z "$pve_guest" ]] || die "Незавершённую VM 410 не удалось удалить"

        tofu -chdir="$OPENTOFU_DIR" state rm "$TARGET_RESOURCE"
        ssh-keygen -R "$guest_ip" -f "$KNOWN_HOSTS" >/dev/null 2>&1 || true

        state_present=0
        state_status=""
        ok "Незавершённая VM 410 удалена; OpenTofu state очищен"
    fi
elif ((state_present == 1)); then
    die "VM 410 есть в OpenTofu state, но отсутствует в PVE"
fi

info "План OpenTofu для 410"
tofu -chdir="$OPENTOFU_DIR" plan \
    -input=false \
    -no-color \
    -lock-timeout=30s \
    -target="$TARGET_RESOURCE" \
    -out="$PLAN_FILE"

plan_json="$(tofu -chdir="$OPENTOFU_DIR" show -json "$PLAN_FILE")"

unexpected_resources="$(
    jq -r --arg target "$TARGET_RESOURCE" '
        .resource_changes[]?.address
        | select(. != $target)
    ' <<<"$plan_json"
)"
[[ -z "$unexpected_resources" ]] || {
    printf '%s\n' "$unexpected_resources" >&2
    die "План 410 содержит изменения других ресурсов"
}

target_actions="$(
    jq -r --arg target "$TARGET_RESOURCE" '
        .resource_changes[]?
        | select(.address == $target)
        | .change.actions[]
    ' <<<"$plan_json"
)"

tofu -chdir="$OPENTOFU_DIR" show -no-color "$PLAN_FILE"

if grep -qx "create" <<<"$target_actions"; then
    prepare_template_for_clone
fi

if [[ -z "$target_actions" ]] || {
    [[ "$target_actions" == "no-op" ]]
}; then
    ok "OpenTofu: состояние VM 410 уже соответствует конфигурации"
else
    info "Применение состояния VM 410"
    tofu -chdir="$OPENTOFU_DIR" apply \
        -input=false \
        -no-color \
        -lock-timeout=30s \
        "$PLAN_FILE"

    restore_template_protection
    ok "Состояние VM 410 применено"
fi

if ! ssh-keygen -F "$guest_ip" -f "$KNOWN_HOSTS" >/dev/null 2>&1; then
    info "Первичное получение SSH host key 410"
    tmp_known_hosts="$(mktemp)"
    if ! ssh-keyscan -T 10 -H "$guest_ip" >"$tmp_known_hosts" 2>/dev/null; then
        rm -f "$tmp_known_hosts"
        die "Не удалось получить SSH host key 410 по адресу $guest_ip"
    fi
    [[ -s "$tmp_known_hosts" ]] || {
        rm -f "$tmp_known_hosts"
        die "SSH host key 410 не получен"
    }
    cat "$tmp_known_hosts" >>"$KNOWN_HOSTS"
    rm -f "$tmp_known_hosts"
    chmod 0600 "$KNOWN_HOSTS"
    ok "SSH host key 410 принят после создания управляемой VM"
fi

info "Настройка ОС 410 через Ansible"
ANSIBLE_HOST_KEY_CHECKING=True \
ANSIBLE_SSH_ARGS="-o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=yes" \
ansible-playbook \
    -i "$guest_ip," \
    -u root \
    --private-key "$ANSIBLE_PRIVATE_KEY" \
    -e "guest=$GUEST_DIR" \
    "$PLAYBOOK"

ok "410 ai-control создан и базово настроен"
