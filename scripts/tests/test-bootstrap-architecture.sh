#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUEST_DIR="$(
    python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys
import yaml

root = Path(sys.argv[1])
matches = []
for manifest in sorted((root / "infrastructure/guests").glob("*/guest.yaml")):
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("role") == "infra-manager":
        matches.append(manifest.parent)
if len(matches) != 1:
    raise SystemExit("Ожидается ровно один гость с role: infra-manager")
print(matches[0])
PY
)"
GUEST="$GUEST_DIR/guest.yaml"
PROVISION="$GUEST_DIR/provision.yaml"
STATUS="$GUEST_DIR/status.yaml"
DEPLOY="$ROOT/scripts/infra-manager/jobs/deploy-guest.py"
GUEST_OPERATIONS="$ROOT/scripts/infra-manager/infra_manager/guest_operations.py"
PLAYBOOK="$ROOT/automation/ansible/playbooks/configure-guest.yml"
BOOTSTRAP_DIR="$ROOT/scripts/bootstrap-runner"

die() {
    printf 'ОШИБКА: %s\n' "$*" >&2
    exit 1
}

[[ -s "$GUEST" ]] || die "У infra-manager отсутствует guest.yaml"
[[ -s "$PROVISION" ]] || die "У infra-manager отсутствует provision.yaml"
[[ -s "$STATUS" ]] || die "У infra-manager отсутствует status.yaml"
[[ -s "$PLAYBOOK" ]] || die "Отсутствует общий Ansible playbook"
[[ -s "$DEPLOY" ]] || die "Отсутствует общая команда deploy-guest"

python3 - "$GUEST" "$PROVISION" <<'PY'
from pathlib import Path
import sys
import yaml

guest_path, provision_path = map(Path, sys.argv[1:])
guest = yaml.safe_load(guest_path.read_text(encoding="utf-8"))
provision = yaml.safe_load(provision_path.read_text(encoding="utf-8"))

vmid = guest.get("vmid")
if not isinstance(vmid, int) or vmid <= 0:
    raise SystemExit("guest.yaml infra-manager должен содержать корректный VMID")
if guest.get("role") != "infra-manager":
    raise SystemExit("guest.yaml должен описывать роль infra-manager")
if guest.get("profile") != "debian-lxc-docker":
    raise SystemExit("infra-manager должен использовать общий профиль debian-lxc-docker")
if guest.get("pve_management") is not False:
    raise SystemExit("infra-manager должен быть исключён только из постоянного PVE-state")
description = str(guest.get("description", ""))
if "owner=proxmox-project" not in description or "role=infra-manager" not in description:
    raise SystemExit("guest.yaml infra-manager должен содержать строгую метку владения")
if provision.get("guest_vmid") != vmid:
    raise SystemExit("provision.yaml должен принадлежать текущему VMID infra-manager")
PY

[[ -s "$BOOTSTRAP_DIR/bootstrap-host.py" ]] || die "Отсутствует Python-оркестратор bootstrap-host.py"
for module in persistence access infra cleanup constants errors; do
    [[ -s "$BOOTSTRAP_DIR/bootstrap_runner/$module.py" ]] \
        || die "Отсутствует модуль bootstrap_runner/$module.py"
done
[[ ! -e "$BOOTSTRAP_DIR/bootstrap-host.sh" ]] || die "Лишняя shell-оболочка bootstrap-host.sh не должна возвращаться"
[[ -s "$BOOTSTRAP_DIR/deploy-infra-manager.sh" ]] || die "Отсутствует сценарий deploy-infra-manager.sh"
[[ ! -e "$BOOTSTRAP_DIR/deploy-910.sh" ]] || die "Сценарий с VMID в имени не должен возвращаться"

grep -q 'deploy_guest' "$DEPLOY"     || die "bootstrap должен использовать общий механизм Deploy"
grep -q 'run_deploy_guest' "$GUEST_OPERATIONS"     || die "общий Deploy должен использовать guest_deploy"
grep -q 'provision.yaml' "$PLAYBOOK"     || die "Общий Ansible playbook должен применять provision.yaml"

if grep -R -n -E     'scripts/infra-manager/setup\.sh|python3[[:space:]]+-m[[:space:]]+infra_manager[[:space:]]+setup|infra_manager[[:space:]]+setup'     "$BOOTSTRAP_DIR"; then
    die "990 не должен вызывать отдельный setup для infra-manager"
fi

grep -q -- '--infrastructure-only' "$BOOTSTRAP_DIR/deploy-infra-manager.sh"     || die "990 должен создавать infra-manager через общую инфраструктурную фазу deploy-guest"
grep -q -- '--provision-base-only' "$BOOTSTRAP_DIR/deploy-infra-manager.sh"     || die "990 должен готовить базовый Debian через общий Ansible"
grep -q -- '--provision-only' "$BOOTSTRAP_DIR/deploy-infra-manager.sh"     || die "990 должен полностью применять provision.yaml через общий Ansible"
grep -q -- '--provision-existing-only' "$BOOTSTRAP_DIR/deploy-infra-manager.sh"     || die "990 должен уметь обновлять существующий infra-manager без собственного state"
grep -q -- '--bootstrap-scope' "$BOOTSTRAP_DIR/deploy-infra-manager.sh"     || die "infra-manager должен оставаться в отдельной bootstrap-области состояния"

grep -q 'name: linux_base' "$PLAYBOOK"     || die "Общий playbook должен подключать общую роль linux_base"
grep -q 'name: guest_layout' "$PLAYBOOK"     || die "Общий playbook должен подключать общую роль guest_layout"
grep -q 'name: docker' "$PLAYBOOK"     || die "Общий playbook должен подключать общую роль Docker"
grep -q 'name: infra_manager' "$PLAYBOOK"     || die "Общий playbook должен подключать роль infra_manager по provision.yaml"

printf '[ОК] Единый Ansible-путь infra-manager зафиксирован\n'
