#!/usr/bin/env bash
# No Python host driver is needed for independent storage safety checks.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tasks="$ROOT/automation/ansible/roles/nas/tasks/verified-storage.yml"
playbook="$ROOT/automation/ansible/playbooks/deploy-nas.yml"
test -f "$tasks"
for needle in   'ansible.builtin.stat:'   'delegate_to: localhost'   'nas_confirmed_state:'   'ansible.builtin.assert:'   'ansible.builtin.command:'   'lsblk' 'findmnt' 'zpool'   'nas_storage_state_file.stat.mode'   'nas_storage.policies.unverified_devices'   'pool_guid' 'filesystem_uuid'; do
  grep -Fq "$needle" "$tasks" || {
    printf '[ОШИБКА] Нет обязательной безопасной проверки %s\n' "$needle" >&2
    exit 1
  }
done
grep -Fq 'tasks_from: verified-storage.yml' "$playbook"
grep -Fq "when: nas_deploy_phase == 'services'" "$playbook"
# All remote probes are read-only, without shell-escaping user parameters.
if grep -Eq 'ansible[.]builtin[.](shell|script|raw):|\b(mkfs|sgdisk|wipefs|zpool create|zpool destroy|zfs destroy)\b' "$tasks"; then
  echo '[ОШИБКА] Проверка идентичности содержит меняющие команды' >&2
  exit 1
fi
echo '[ОК] Ansible: защита UUID/GUID и дисков до служб, без Python-драйвера'
