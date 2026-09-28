#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUEST="$ROOT/infrastructure/guests/910-infra-manager/guest.yaml"
PROVISION="$ROOT/infrastructure/guests/910-infra-manager/provision.yaml"
DEPLOY="$ROOT/scripts/infra-manager/jobs/deploy-guest.py"
PLAYBOOK="$ROOT/automation/ansible/playbooks/configure-guest.yml"
BOOTSTRAP_DIR="$ROOT/scripts/bootstrap-runner"

die() {
    printf 'ОШИБКА: %s\n' "$*" >&2
    exit 1
}

[[ -s "$GUEST" ]] || die "У 910 отсутствует guest.yaml"
[[ -s "$PROVISION" ]] || die "У 910 отсутствует provision.yaml"
[[ -s "$PLAYBOOK" ]] || die "Отсутствует общий Ansible playbook"
[[ -s "$DEPLOY" ]] || die "Отсутствует общая команда deploy-guest"

python3 - "$GUEST" "$PROVISION" <<'PY'
from pathlib import Path
import sys
import yaml

guest_path, provision_path = map(Path, sys.argv[1:])
guest = yaml.safe_load(guest_path.read_text(encoding="utf-8"))
provision = yaml.safe_load(provision_path.read_text(encoding="utf-8"))

if guest.get("vmid") != 910:
    raise SystemExit("guest.yaml должен описывать VMID 910")
if guest.get("profile") != "debian-lxc-docker":
    raise SystemExit("910 должен использовать общий профиль debian-lxc-docker")
if guest.get("pve_management") is not False:
    raise SystemExit("910 должен быть исключён только из постоянного PVE-state")
if provision.get("guest_vmid") != 910:
    raise SystemExit("provision.yaml должен принадлежать VMID 910")
PY

[[ -s "$BOOTSTRAP_DIR/bootstrap-host.py" ]] || die "Отсутствует Python-оркестратор bootstrap-host.py"
[[ -s "$BOOTSTRAP_DIR/bootstrap-host.sh" ]] || die "Отсутствует минимальная shell-оболочка bootstrap-host.sh"

grep -q 'exec python3 .*bootstrap-host.py' "$BOOTSTRAP_DIR/bootstrap-host.sh"     || die "bootstrap-host.sh должен только передавать управление Python"

if grep -Eq 'pveum|pct[[:space:]]+(config|exec|destroy|set|stop|start)|deploy-910|pve-access|infra-manager-status' "$BOOTSTRAP_DIR/bootstrap-host.sh"; then
    die "bootstrap-host.sh не должен содержать оркестрацию"
fi

grep -q 'run_deploy_guest' "$DEPLOY"     || die "910 должен разворачиваться через общий deploy-guest"
grep -q 'provision.yaml' "$PLAYBOOK"     || die "Общий Ansible playbook должен применять provision.yaml"

if grep -R -n -E     'scripts/infra-manager/setup\.sh|python3[[:space:]]+-m[[:space:]]+infra_manager[[:space:]]+setup|infra_manager[[:space:]]+setup'     "$BOOTSTRAP_DIR"; then
    die "990 не должен вызывать отдельный setup для 910"
fi

grep -q -- '--infrastructure-only' "$BOOTSTRAP_DIR/deploy-910.sh"     || die "990 должен создавать 910 через общую инфраструктурную фазу deploy-guest"
grep -q -- '--provision-base-only' "$BOOTSTRAP_DIR/deploy-910.sh"     || die "990 должен готовить базовый Debian через общий Ansible"
grep -q -- '--provision-only' "$BOOTSTRAP_DIR/deploy-910.sh"     || die "990 должен полностью применять provision.yaml через общий Ansible"
grep -q -- '--provision-existing-only' "$BOOTSTRAP_DIR/deploy-910.sh"     || die "990 должен уметь обновлять существующий 910 без собственного state"
grep -q -- '--bootstrap-scope' "$BOOTSTRAP_DIR/deploy-910.sh"     || die "910 должен оставаться в отдельной bootstrap-области состояния"

grep -q 'configure-docker.yml' "$PLAYBOOK"     || die "Общий playbook должен подключать общий модуль Docker"
grep -q 'configure-infra-runtime.yml' "$PLAYBOOK"     || die "Общий playbook должен подключать модуль infra-runtime по provision.yaml"

printf '[ОК] Единый Ansible-путь 910 зафиксирован\n'
