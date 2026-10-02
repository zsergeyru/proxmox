#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANSIBLE_CONFIG="$ROOT/ansible.cfg"
ANSIBLE_PLAYBOOK="$ROOT/automation/ansible/playbooks/configure-guest.yml"
ANSIBLE_LINUX_BASE="$ROOT/automation/ansible/roles/linux_base/tasks/main.yml"
PROJECT_GIT_TASKS="$ROOT/automation/ansible/roles/linux_base/tasks/project_git.yml"
ANSIBLE_GUEST_LAYOUT="$ROOT/automation/ansible/roles/guest_layout/tasks/main.yml"
ANSIBLE_DOCKER="$ROOT/automation/ansible/roles/docker/tasks/main.yml"
ANSIBLE_RUNTIME_DIR="$ROOT/automation/ansible/roles/infra_manager/tasks"
ANSIBLE_VERIFY="$ANSIBLE_RUNTIME_DIR/verify.yml"
ANSIBLE_RUNTIME_MAIN="$ANSIBLE_RUNTIME_DIR/main.yml"
BOOTSTRAP_HOST="$ROOT/scripts/bootstrap-runner/bootstrap-host.py"
BOOTSTRAP_INFRA="$ROOT/scripts/bootstrap-runner/bootstrap_runner/infra.py"
PY_SETTINGS="$ROOT/scripts/infra-manager/infra_manager/settings.py"
PY_SEMAPHORE="$ROOT/scripts/infra-manager/infra_manager/semaphore.py"
PY_STATUS="$ROOT/scripts/infra-manager/infra_manager/status.py"
PY_PVE="$ROOT/scripts/infra-manager/infra_manager/pve.py"
PY_LIFECYCLE="$ROOT/scripts/infra-manager/infra_manager/pve_lifecycle.py"
PY_COMMON="$ROOT/scripts/infra-manager/infra_manager/common.py"
PYTHON_COMMAND="$ROOT/scripts/infra-manager/commands/python-command.sh"
ACTIVATE_RUNTIME="$ROOT/scripts/infra-manager/commands/activate-runtime.sh"
BUILD_TEMPLATE="$ROOT/scripts/infra-manager/jobs/build-template.py"
DEPLOY_GUEST="$ROOT/scripts/infra-manager/jobs/deploy-guest.py"
PY_OPENTOFU="$ROOT/scripts/infra-manager/infra_manager/opentofu.py"
PY_TEMPLATE="$ROOT/scripts/infra-manager/infra_manager/template.py"
PY_TEMPLATE_BUILD="$ROOT/scripts/infra-manager/infra_manager/template_build.py"
PY_TEMPLATE_VERIFY="$ROOT/scripts/infra-manager/infra_manager/template_verify.py"
COMPOSE="$ROOT/infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/docker-compose.yml"
OPENBAO_CONFIG="$ROOT/infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/openbao/openbao.hcl"
OPENBAO_HOST="$ROOT/scripts/infra-manager/host/openbao-unseal.py"
OPENBAO_HOST_PACKAGE="$ROOT/scripts/infra-manager/host/openbao_host"
OPENBAO_STARTUP_COMMAND="$ROOT/scripts/infra-manager/commands/openbao-startup-unseal.sh"
OPENBAO_STARTUP_SERVICE="$ROOT/infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2"
OPENBAO_JOB="$ROOT/scripts/infra-manager/jobs/initialize-openbao.py"
SSH_ACCESS_JOB="$ROOT/scripts/infra-manager/jobs/sync-ssh-access.py"
SSH_ACCESS_ACCEPTANCE="$ROOT/scripts/acceptance/verify-ssh-access.py"
PY_ACCESS_POLICY="$ROOT/scripts/infra-manager/infra_manager/access.py"
PY_OPENBAO="$ROOT/scripts/infra-manager/infra_manager/openbao.py"
PY_RECOVERY="$ROOT/scripts/infra-manager/infra_manager/recovery.py"
RECOVERY_HOST="$ROOT/scripts/infra-manager/host/recovery.py"
DOCKERFILE="$ROOT/infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/runtime/Dockerfile"
REQ="$ROOT/infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/runtime/requirements.txt"
PLAN="$ROOT/scripts/infra-manager/jobs/opentofu-plan.py"
PY_PVE_HOST="$ROOT/scripts/infra-manager/infra_manager/pve_host.py"
OPENTOFU_LOCK="$ROOT/automation/opentofu/.terraform.lock.hcl"
GUEST_MANIFEST="$ROOT/infrastructure/guests/910-infra-manager/guest.yaml"
PROVISION="$ROOT/infrastructure/guests/910-infra-manager/provision.yaml"
SSH_CONFIG="$ROOT/infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/runtime/ssh_config"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

ANSIBLE_RUNTIME_PARTS=(
    persistence.yml
    pve_access.yml
    ansible_access.yml
    repository.yml
    semaphore.yml
    runtime.yml
    openbao.yml
    recovery.yml
    verify.yml
)

[[ -s "$ANSIBLE_RUNTIME_MAIN" ]] || die "Отсутствует основной файл роли infra_manager"
for task_file in "${ANSIBLE_RUNTIME_PARTS[@]}"; do
    [[ -s "$ANSIBLE_RUNTIME_DIR/$task_file" ]] \
        || die "Отсутствует часть роли infra_manager: $task_file"
    grep -Fq "ansible.builtin.import_tasks: $task_file" "$ANSIBLE_RUNTIME_MAIN" \
        || die "main.yml роли infra_manager не подключает $task_file"
done

ANSIBLE_RUNTIME="$(mktemp)"
trap 'rm -f "$ANSIBLE_RUNTIME"' EXIT
cat "$ANSIBLE_RUNTIME_MAIN" > "$ANSIBLE_RUNTIME"
for task_file in "${ANSIBLE_RUNTIME_PARTS[@]}"; do
    cat "$ANSIBLE_RUNTIME_DIR/$task_file" >> "$ANSIBLE_RUNTIME"
done
grep -q '^PROJECT_ID_FILE = PATHS.semaphore_project_id_file' "$PY_SEMAPHORE" \
    || die "Semaphore должен читать путь project-id из единых путей"
grep -q '^PROJECT_ID_FILE = PATHS.semaphore_project_id_file' "$PY_STATUS" \
    || die "Python status должен читать путь project-id из единых путей"
grep -q 'return self.data_dir / "semaphore-project-id"' "$PY_SETTINGS" \
    || die "Semaphore project-id должен храниться вне каталога SQLite"
if grep -q '/var/lib/infra-manager/semaphore/project-id' "$PY_SEMAPHORE" "$PY_STATUS"; then
    die "project-id запрещено хранить внутри каталога SQLite Semaphore"
fi

grep -q '/etc/semaphore/requirements.txt' "$DOCKERFILE" \
    || die "Semaphore Dockerfile должен устанавливать Python requirements"
grep -q '^proxmoxer' "$REQ" \
    || die "Semaphore должен содержать proxmoxer"

grep -q '"--full"' "$ROOT/scripts/infra-manager/infra_manager/cli.py" \
    || die "Python status должен поддерживать --full"
grep -q 'GitHub project read-only' "$PY_STATUS" \
    || die "Python status должен проверять GitHub SSH key Semaphore"
grep -q 'PROJECT_REPO' "$PY_STATUS" \
    || die "Python status должен проверять Git repository Semaphore"
grep -q '/branches' "$PY_STATUS" \
    || die "Python status должен реально проверять доступ Semaphore к Git repository"
grep -q '"1001:0"' "$PY_STATUS" \
    || die "Python status должен готовить каталог ssh-agent от uid 1001"
grep -q 'PveClient' "$PY_STATUS" \
    || die "Базовый Python status должен реально проверять PVE API credential"
grep -q 'check_access(quiet=True)' "$PY_STATUS" \
    || die "status --full должен проверять полный контракт PVE API"
grep -Fq 'SETTINGS.infra_manager_env_name' "$PY_STATUS" \
    || die "status должен проверять Variable Group Infra Manager"

grep -q 'ROOT_ADMIN_PRIVS = {' "$PY_PVE" \
    || die "Python PVE access check должен содержать административный контракт"
grep -Fq 'require_permissions(client, "/", ROOT_ADMIN_PRIVS)' "$PY_PVE" \
    || die "Полная проверка PVE access должна требовать права root@pam"
grep -q 'check_root_access(node)' "$PY_PVE" \
    || die "Полная проверка PVE access должна проверять root SSH"

if grep -q '/etc/pve/' "$ANSIBLE_PLAYBOOK" "$ANSIBLE_LINUX_BASE" "$ANSIBLE_GUEST_LAYOUT" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Настройка внутри infra-manager не должна работать с файловой системой /etc/pve"
fi

grep -q 'run_build_template' "$BUILD_TEMPLATE" \
    || die "Build Template должен передавать выполнение Python-модулю"
grep -q 'infra_manager.template_build import run_build_template' "$BUILD_TEMPLATE" \
    || die "Build Template должен использовать отдельный модуль сборки"
grep -q '^TEMPLATE_VERSION = 8' "$PY_TEMPLATE" \
    || die "Python-сборка должна работать с Template-Version 8"
grep -q 'run_verify_template(vmid)' "$PY_TEMPLATE_BUILD" \
    || die "После сборки должен запускаться короткий Full Clone test"
grep -q '^TEST_VMID = 9099' "$PY_TEMPLATE_VERIFY" \
    || die "Проверка шаблона 9000 должна использовать VMID 9099"
grep -q 'run_deploy_guest' "$DEPLOY_GUEST" \
    || die "Deploy Guest должен передавать выполнение Python-модулю"
grep -Fq 'reserve_runtime_activation()' "$DEPLOY_GUEST" \
    || die "Самообновление infra-manager должно резервировать окно активации runtime"
grep -Fq 'cancel_runtime_activation()' "$DEPLOY_GUEST" \
    || die "Неуспешное самообновление infra-manager должно снимать резерв активации"
for job in "$DEPLOY_GUEST" "$OPENBAO_JOB" "$PLAN" "$BUILD_TEMPLATE"; do
    grep -Fq 'require_runtime_activation_idle()' "$job" \
        || die "Infrastructure job должен блокироваться во время активации runtime: $job"
done

grep -q 'run_plan' "$PLAN" \
    || die "OpenTofu Plan должен передавать выполнение Python-модулю"
grep -q '"-detailed-exitcode"' "$PY_OPENTOFU" \
    || die "OpenTofu Plan должен различать наличие изменений"
grep -q '"-lockfile=readonly"' "$PY_OPENTOFU" \
    || die "OpenTofu должен использовать только зафиксированный lock file"
if grep -Eq '^[[:space:]]+keyctl[[:space:]]*=' "$ROOT/automation/opentofu/main.tf"; then
    die "OpenTofu не должен передавать keyctl через PVE API token"
fi
grep -Fq 'ignore_changes = [features[0].keyctl]' "$ROOT/automation/opentofu/main.tf" \
    || die "OpenTofu должен игнорировать keyctl, которым управляет host-only слой"

grep -q 'provider "registry.opentofu.org/bpg/proxmox"' "$OPENTOFU_LOCK" \
    || die "OpenTofu lock file должен фиксировать bpg/proxmox из OpenTofu Registry"
grep -q 'version     = "0.112.0"' "$OPENTOFU_LOCK" \
    || die "OpenTofu lock file должен фиксировать bpg/proxmox 0.112.0"

grep -q 'TF_VAR_pve_endpoint' "$PY_SEMAPHORE" \
    || die "Variable Group должен передавать pve_endpoint"
grep -q 'TF_VAR_pve_api_token' "$PY_SEMAPHORE" \
    || die "Variable Group должен передавать pve_api_token"
grep -Fq 'secret_payload["operation"] = "update"' "$PY_SEMAPHORE" \
    || die "Существующий PVE token в Variable Group должен синхронизироваться"
grep -q '"override_secret": True' "$PY_SEMAPHORE" \
    || die "Существующий GitHub SSH key должен обновлять секретную часть"
grep -q '{"id": repository_id, \*\*payload}' "$PY_SEMAPHORE" \
    || die "PUT Git repository должен передавать repository id в теле"

python3 - "$COMPOSE" <<'PY'
from pathlib import Path
import sys, yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text(encoding='utf-8'))
services = data.get('services', {})
if set(services) != {'runtime', 'openbao'}:
    raise SystemExit(f'unexpected services: {sorted(services)}')

runtime = services['runtime']
openbao = services['openbao']

if runtime.get('image') != 'infra-runtime:${RUNTIME_VERSION}':
    raise SystemExit('infra-runtime image must use pinned runtime version variable')
if runtime.get('container_name') != 'infra-runtime':
    raise SystemExit('infra-runtime must use the canonical container name')
if runtime.get('network_mode') != 'host':
    raise SystemExit('infra-runtime must use host network')
if openbao.get('image') != 'ghcr.io/openbao/openbao:${OPENBAO_VERSION}':
    raise SystemExit('OpenBao image must use pinned version variable')
if openbao.get('container_name') != 'openbao':
    raise SystemExit('OpenBao must use the canonical container name')
if openbao.get('command') != ['server']:
    raise SystemExit('OpenBao must run in normal server mode')

volumes = runtime.get('volumes', [])
required = {
    '/mnt/persistent-state/semaphore:/var/lib/semaphore',
    '/mnt/persistent-state/opentofu:/var/lib/infra-manager/opentofu',
    '/etc/infra-manager/ca:/etc/infra-manager/ca:ro',
    '/mnt/persistent-state/ansible:/etc/infra-manager/ansible:ro',
    '/mnt/pve-access/pve-host:/mnt/pve-access/pve-host:ro',
}
missing = required.difference(volumes)
if missing:
    raise SystemExit(f'missing Semaphore volumes: {sorted(missing)}')

for volume in volumes:
    if 'pve-api.env' in volume:
        raise SystemExit('PVE API secret must not be bind-mounted directly into Semaphore')
    if 'public-keys' in volume:
        raise SystemExit('Unused Ansible public-key volume must not be mounted into Semaphore')
PY

if grep -RInE '(^|[^0-9])910([^0-9]|$)|192\.168\.9\.10|910-infra-manager' \
    --include='*.py' --include='*.sh' --include='*.yml' --include='*.yaml' --include='*.j2' \
    "$ROOT/scripts/bootstrap-runner" \
    "$ROOT/scripts/infra-manager" \
    "$ROOT/scripts/acceptance" \
    "$ROOT/scripts/maintenance" \
    "$ROOT/automation/ansible"
then
    die "Общая исполняемая логика не должна зависеть от VMID 910 или адреса 192.168.9.10"
fi

ok "Контракт setup infra-manager проверен"
