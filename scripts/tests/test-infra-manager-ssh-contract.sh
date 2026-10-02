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

LINUX_BASE_SSH_TRUST="$ROOT/automation/ansible/roles/linux_base/tasks/ssh_trust.yml"
[[ -f "$LINUX_BASE_SSH_TRUST" ]] \
    || die "Роль linux_base должна настраивать доверие SSH к OpenBao"
grep -Fq 'TrustedUserCAKeys /etc/ssh/trusted-user-ca-keys.pem' "$LINUX_BASE_SSH_TRUST" \
    || die "Гости должны доверять пользовательскому SSH CA OpenBao"
grep -Fq '/etc/infra-manager/ca/ssh-client-ca.pub' "$LINUX_BASE_SSH_TRUST" \
    || die "Ansible должен брать открытый SSH CA из каталога 910"
grep -Fq 'name: ssh.socket' "$LINUX_BASE_SSH_TRUST" \
    || die "Debian 13 должен отключать socket-активацию SSH"
grep -Fq 'name: ssh.service' "$LINUX_BASE_SSH_TRUST" \
    || die "Debian 13 должен использовать обычную службу SSH"
grep -Fq 'when: not (infra_self_update | default(false) | bool)' "$ROOT/automation/ansible/roles/infra_manager/tasks/verify.yml" \
    || die "Самообновление 910 не должно менять Semaphore внутри текущего задания"
grep -Fq 'semaphore-project' "$ACTIVATE_RUNTIME" \
    || die "Самообновление 910 должно откладывать синхронизацию Semaphore до активации новой среды"
grep -Fq 'infra-manager-status --full --quiet' "$ACTIVATE_RUNTIME" \
    || die "Самообновление 910 должно выполнять полную status-проверку после активации новой среды"
    || die "Отложенная активация должна синхронизировать Semaphore после запуска новой среды"
grep -Fq 'Назначить активацию новой управляющей среды после настройки infra-manager' "$ANSIBLE_PLAYBOOK" \
    || die "Активация новой среды должна назначаться в конце общего playbook"
if grep -Fq 'state: reloaded' "$LINUX_BASE_SSH_TRUST"; then
    die "Reload ssh.service запрещён из-за Debian 13 LXC + ssh.socket"
fi

