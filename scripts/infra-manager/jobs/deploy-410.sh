#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
OPENTOFU_DIR="$REPO_ROOT/automation/opentofu"
STATE_DIR="/var/lib/infra-manager/opentofu"
GUEST_STATE_FILE="$STATE_DIR/guests.json"
PLAN_FILE="$STATE_DIR/410.tfplan"
ANSIBLE_PRIVATE_KEY="/etc/infra-manager/ansible/guest_ed25519"
KNOWN_HOSTS="/var/lib/infra-manager/semaphore/guest-known-hosts"
PLAYBOOK="$REPO_ROOT/automation/ansible/playbooks/configure-guest.yml"
GUEST_DIR="410-ai-control"
TARGET_RESOURCE='proxmox_virtual_environment_vm.guest["410"]'

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }

cleanup() {
    rm -f "$PLAN_FILE"
}
trap cleanup EXIT

for command in python3 jq tofu ansible-playbook ssh-keygen ssh-keyscan; do
    command -v "$command" >/dev/null 2>&1 || die "Не найден $command"
done

[[ -n "${TF_VAR_pve_endpoint:-}" ]] || die "Не задан TF_VAR_pve_endpoint"
[[ -n "${TF_VAR_pve_api_token:-}" ]] || die "Не задан TF_VAR_pve_api_token"
[[ -n "${TF_VAR_ansible_ssh_public_key:-}" ]] || die "Не задан TF_VAR_ansible_ssh_public_key"
[[ -r "$ANSIBLE_PRIVATE_KEY" ]] || die "Не найден закрытый ключ Ansible"
[[ -f "$PLAYBOOK" ]] || die "Не найден playbook $PLAYBOOK"

install -d -m 0750 "$STATE_DIR"
touch "$KNOWN_HOSTS"
chmod 0600 "$KNOWN_HOSTS"
umask 077

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

info "Инициализация OpenTofu"
tofu -chdir="$OPENTOFU_DIR" init     -input=false     -no-color     -lockfile=readonly

info "План OpenTofu для 410"
tofu -chdir="$OPENTOFU_DIR" plan     -input=false     -no-color     -lock-timeout=30s     -target="$TARGET_RESOURCE"     -out="$PLAN_FILE"

unexpected_resources="$(
    tofu -chdir="$OPENTOFU_DIR" show -json "$PLAN_FILE"       | jq -r --arg target "$TARGET_RESOURCE" '
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
tofu -chdir="$OPENTOFU_DIR" apply     -input=false     -no-color     -lock-timeout=30s     "$PLAN_FILE"

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
ANSIBLE_HOST_KEY_CHECKING=True ANSIBLE_SSH_ARGS="-o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=yes" ansible-playbook     -i "$guest_ip,"     -u root     --private-key "$ANSIBLE_PRIVATE_KEY"     -e "guest=$GUEST_DIR"     "$PLAYBOOK"

ok "410 ai-control создан и базово настроен"
