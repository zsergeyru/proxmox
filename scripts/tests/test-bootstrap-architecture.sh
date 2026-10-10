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
BOOTSTRAP_DEPLOY="$ROOT/scripts/infra-manager/jobs/bootstrap-infra-manager.py"
PY_GUEST_DEPLOY="$ROOT/scripts/infra-manager/infra_manager/guest_deploy.py"
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
for module in persistence access infra cleanup constants errors role paths commands; do
    [[ -s "$BOOTSTRAP_DIR/bootstrap_runner/$module.py" ]] \
        || die "Отсутствует модуль bootstrap_runner/$module.py"
done
[[ ! -e "$BOOTSTRAP_DIR/bootstrap-host.sh" ]] || die "Лишняя shell-оболочка bootstrap-host.sh не должна возвращаться"
[[ ! -e "$BOOTSTRAP_DIR/deploy-infra-manager.sh" ]] || die "Отдельная deploy shell-обёртка infra-manager не должна возвращаться"
[[ ! -e "$BOOTSTRAP_DIR/deploy-910.sh" ]] || die "Сценарий с VMID в имени не должен возвращаться"

grep -q 'run_deploy_guest' "$DEPLOY"     || die "Обычный deploy должен использовать общий run_deploy_guest"
[[ -s "$BOOTSTRAP_DEPLOY" ]] || die "Отсутствует отдельная внутренняя bootstrap-точка 910"
grep -q 'run_bootstrap_infra_manager_step' "$BOOTSTRAP_DEPLOY" || die "Bootstrap 910 должен использовать отдельный внутренний интерфейс"
grep -q 'BOOTSTRAP_INFRA_MANAGER_PHASES' "$BOOTSTRAP_DEPLOY" || die "Bootstrap job должен брать допустимые шаги из единого контракта"
grep -q 'choices=tuple(BOOTSTRAP_INFRA_MANAGER_PHASES)' "$BOOTSTRAP_DEPLOY" || die "Bootstrap job не должен дублировать список внутренних шагов"
grep -q '"configure-recovery-control-plane": "provision-recovery-control-plane"' "$PY_GUEST_DEPLOY" || die "Единый bootstrap-контракт должен содержать recovery-этап OpenBao"
grep -q 'provision.yaml' "$PLAYBOOK"     || die "Общий Ansible playbook должен применять provision.yaml"

if grep -q -E -- '--bootstrap-scope|--infrastructure-only|--provision-base-only|--provision-only|--provision-existing-only' "$DEPLOY"; then
    die "Публичный deploy-guest не должен содержать bootstrap-фазы 910"
fi

if grep -R -n -E     'scripts/infra-manager/setup\.sh|python3[[:space:]]+-m[[:space:]]+infra_manager[[:space:]]+setup|infra_manager[[:space:]]+setup'     "$BOOTSTRAP_DIR"; then
    die "990 не должен вызывать отдельный setup для infra-manager"
fi

grep -q 'run-runtime.sh' "$BOOTSTRAP_DIR/bootstrap_runner/infra.py" || die "990 должен вызывать runtime напрямую"
grep -q 'bootstrap-infra-manager.py' "$BOOTSTRAP_DIR/bootstrap_runner/infra.py" || die "990 должен вызывать внутреннюю Python-точку bootstrap"
if grep -R -q 'runner_owns_infra\|handoff_existing_infra' "$BOOTSTRAP_DIR"; then
    die "990 не должен владеть существующим 910 через старый bootstrap-state"
fi
if grep -R -q 'ensure_runner_ssh_access_to_infra' "$BOOTSTRAP_DIR"; then
    die "990 не должен вручную добавлять отдельный SSH-ключ в authorized_keys 910"
fi
grep -q 'infra_manager_ed25519' "$BOOTSTRAP_DIR/prepare-runtime.sh" || die "Bootstrap 910 должен иметь один явно названный одноразовый SSH-ключ"
grep -q 'infra-manager-bootstrap' "$BOOTSTRAP_DIR/prepare-runtime.sh" || die "Bootstrap SSH key 910 должен иметь единый маркер"

grep -q 'name: linux_base' "$PLAYBOOK"     || die "Общий playbook должен подключать общую роль linux_base"
grep -q 'name: guest_layout' "$PLAYBOOK"     || die "Общий playbook должен подключать общую роль guest_layout"
grep -q 'name: docker' "$PLAYBOOK"     || die "Общий playbook должен подключать общую роль Docker"
grep -q 'name: infra_manager' "$PLAYBOOK"     || die "Общий playbook должен подключать роль infra_manager по provision.yaml"

# Обычный deploy новых гостей должен сохранить собственный одноразовый bootstrap.
grep -q 'bootstrap_scope=False' "$ROOT/scripts/infra-manager/infra_manager/guest_deploy.py" \
    || die "Обычный deploy должен оставаться вне bootstrap-области infra-manager"
grep -q '_ensure_bootstrap_identity(context.vmid)' "$ROOT/scripts/infra-manager/infra_manager/guest_deploy.py" \
    || die "Обычный новый гость должен получать одноразовый bootstrap SSH key"
grep -q '_remove_bootstrap_identity(context.vmid)' "$ROOT/scripts/infra-manager/infra_manager/guest_deploy.py" \
    || die "Обычный deploy должен удалять одноразовый bootstrap SSH key после настройки"

# Контракт SSH-ключей bootstrap 910.
PREPARE_RUNTIME="$BOOTSTRAP_DIR/prepare-runtime.sh"
RUN_RUNTIME="$BOOTSTRAP_DIR/run-runtime.sh"
CLEANUP="$BOOTSTRAP_DIR/bootstrap_runner/cleanup.py"
INFRA_RUNNER="$BOOTSTRAP_DIR/bootstrap_runner/infra.py"

grep -q 'rm -f' "$PREPARE_RUNTIME" || die "Новый bootstrap-сеанс должен удалять старую пару ключей перед генерацией"
grep -q 'rm -rf.*opentofu/state' "$PREPARE_RUNTIME" || die "Новый bootstrap-сеанс должен начинаться с пустого временного OpenTofu state"
grep -q 'infra_manager_ed25519' "$PREPARE_RUNTIME" || die "Bootstrap 910 должен использовать единственную сеансовую identity"
grep -q 'ssh-keygen -q -t ed25519' "$PREPARE_RUNTIME" || die "Bootstrap key должен генерироваться через ssh-keygen"
grep -q 'infra-manager-bootstrap' "$PREPARE_RUNTIME" || die "Bootstrap key должен иметь единый сеансовый marker"
grep -q 'INFRA_BOOTSTRAP_SSH_PRIVATE_KEY=.*infra_manager_ed25519' "$RUN_RUNTIME" || die "Runtime должен получать только одноразовый bootstrap key 910"
if grep -q 'guest_ed25519' "$RUN_RUNTIME" "$PREPARE_RUNTIME" "$CLEANUP" "$INFRA_RUNNER" "$PY_GUEST_DEPLOY"; then
    die "Постоянный guest_ed25519 не должен возвращаться в активный deploy/bootstrap-контур"
fi
if grep -R -q 'bootstrap-runner-990' "$BOOTSTRAP_DIR"; then
    die "Отдельный постоянный SSH marker bootstrap-runner-990 не должен возвращаться"
fi
grep -q 'remove_bootstrap_ssh_access_from_infra' "$CLEANUP" || die "Успешный bootstrap обязан удалить authorization одноразового ключа из 910"
grep -q '"/etc/bootstrap-runner/bootstrap-ssh"' "$CLEANUP" || die "Успешный bootstrap обязан удалить локальную bootstrap-пару 990"
grep -q 'allow_legacy_bootstrap=False' "$PY_GUEST_DEPLOY" || die "Обычный self-deploy infra-manager не должен иметь bootstrap fallback"
grep -q '"/var/lib/bootstrap-runner/opentofu/state"' "$INFRA_RUNNER" || die "State шага create должен удаляться сразу после создания 910"
grep -q 'allow_legacy_bootstrap=not bootstrap_scope' "$PY_GUEST_DEPLOY" || die "Финальный bootstrap configure должен запрещать fallback на начальный SSH-ключ"
grep -q 'configure-control-plane' "$PY_GUEST_DEPLOY" || die "Bootstrap должен иметь отдельный этап запуска управляющего контура"
grep -q 'configure-ssh-trust' "$PY_GUEST_DEPLOY" || die "Bootstrap должен иметь отдельный этап перехода на SSH CA"

python3 - "$BOOTSTRAP_DIR/bootstrap-host.py" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
start = source.index("    def apply(self) -> None:")
end = source.index("    def execute(self) -> None:", start)
apply_body = source[start:end]
steps = [
    'self.configure_infra_manager_base)',
    'self.handoff_infra)',
    'self.configure_infra_manager_control_plane',
    'self.initialize_infra_openbao)',
    'self.sync_infra_ssh_ca_to_runner)',
    'self.configure_infra_manager_ssh_trust)',
    'self.remove_bootstrap_ssh_access_from_infra,',
    'self.configure_infra_manager)',
]
positions = [apply_body.index(step) for step in steps]
if positions != sorted(positions):
    raise SystemExit("Нарушен порядок перехода bootstrap 910 на SSH-сертификаты")
PY

printf '[ОК] Граница обычного deploy и bootstrap 910 зафиксирована\n'
