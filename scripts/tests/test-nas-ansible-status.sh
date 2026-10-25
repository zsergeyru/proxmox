#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
playbook="$root/automation/ansible/playbooks/status-nas.yml"
checks="$root/automation/ansible/roles/nas/tasks/declared-status.yml"
test -f "$playbook" && test -f "$checks"
for task in 'ansible.builtin.service_facts:' 'ansible.builtin.command:'   'ansible.builtin.wait_for:' 'ansible.builtin.stat:'   'ansible.builtin.assert:' 'status.yaml' 'nas_status_reference_values'; do
  grep -Fq "$task" "$checks" || { echo "Нет $task" >&2; exit 1; }
done
grep -Fq 'tasks_from: verified-storage.yml' "$playbook"
grep -Fq 'tasks_from: declared-status.yml' "$playbook"
if grep -Eq 'ansible[.]builtin[.](shell|script|raw):|\b(mkfs|wipefs|zpool create|zfs destroy)\b' "$checks"; then
  echo 'В статусе обнаружена недопустимая операция' >&2
  exit 1
fi
echo '[ОК] Статус NAS на чистом Ansible: только объявленные проверки'
