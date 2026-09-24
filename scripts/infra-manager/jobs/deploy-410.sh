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

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }

cleanup() {
    rm -f "$PLAN_FILE"
    unset PVE_TOKEN_SECRET AUTH_HEADER
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
if tofu -chdir="$OPENTOFU_DIR" state show "$TARGET_RESOURCE" >/dev/null 2>&1; then
    state_present=1
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

unexpected_resources="$(
    tofu -chdir="$OPENTOFU_DIR" show -json "$PLAN_FILE" \
      | jq -r --arg target "$TARGET_RESOURCE" '
          .resource_changes[]?.address
          | select(. != $target)
        '
)"
[[ -z "$unexpected_resources" ]] || {
    printf '%s\n' "$unexpected_resources" >&2
    die "План 410 содержит изменения других ресурсов"
}

tofu -chdir="$OPENTOFU_DIR" show -no-color "$PLAN_FILE"

info "Применение состояния VM 410"
tofu -chdir="$OPENTOFU_DIR" apply \
    -input=false \
    -no-color \
    -lock-timeout=30s \
    "$PLAN_FILE"

ok "Состояние VM 410 применено"

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
