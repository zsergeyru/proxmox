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
PVE_OPERATOR="$ROOT/scripts/infra-manager/host/manager.py"
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
[[ -s "$PVE_OPERATOR" ]] \
    || die "Отсутствует единая операторская команда PVE"
grep -Fq '"status"' "$PVE_OPERATOR" \
    || die "PVE-команда infra-manager должна поддерживать status"
grep -Fq '"repair"' "$PVE_OPERATOR" \
    || die "PVE-команда infra-manager должна поддерживать repair"
grep -Fq '"recover"' "$PVE_OPERATOR" \
    || die "PVE-команда infra-manager должна поддерживать recover"
grep -Fq 'source: manager.py' "$ANSIBLE_RUNTIME" \
    || die "Deploy Guest infra-manager должен обновлять PVE-команду infra-manager"
grep -Fq '/usr/local/sbin/infra-manager' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать единую операторскую команду на PVE"
grep -Fq 'path    = "/openbao/file/raft"' "$OPENBAO_CONFIG" \
    || die "OpenBao должен использовать постоянное Raft-хранилище"
grep -Fq 'ui = true' "$OPENBAO_CONFIG" \
    || die "OpenBao должен публиковать встроенный web UI через TLS listener"
grep -Fq 'address         = "127.0.0.1:8200"' "$OPENBAO_CONFIG" \
    || die "OpenBao API должен слушать только loopback 910"
grep -Fq 'tls_disable     = true' "$OPENBAO_CONFIG" \
    || die "Первая локальная версия OpenBao должна явно фиксировать локальный HTTP"
if grep -Eq '(^|[^a-z])dev([^a-z]|$)' "$OPENBAO_CONFIG"; then
    die "OpenBao config не должен содержать dev-режим"
fi
[[ -s "$OPENBAO_HOST_PACKAGE/snippets.py" ]] \
    || die "Пакет OpenBao должен содержать встроенные программы"
[[ -s "$OPENBAO_HOST_PACKAGE/tls.py" ]] \
    || die "Пакет OpenBao должен содержать отдельную TLS-логику"
[[ -s "$OPENBAO_HOST_PACKAGE/errors.py" ]] \
    || die "Пакет OpenBao должен содержать общую ошибку host-слоя"
grep -RFq '"secret_shares": 1' "$OPENBAO_HOST_PACKAGE" \
    || die "Первичная инициализация OpenBao должна создавать один unseal-ключ"
grep -RFq '"secret_threshold": 1' "$OPENBAO_HOST_PACKAGE" \
    || die "Порог разблокировки OpenBao должен быть равен одному ключу"
grep -Fq '/mnt/bindmounts/infra-manager/pve-only/openbao' "$OPENBAO_HOST" \
    || die "Unseal-ключ OpenBao должен храниться только в pve-only каталоге"
grep -Fq 'payload.pop("root_token", None)' "$OPENBAO_HOST" \
    || die "Initial root token OpenBao должен извлекаться без постоянного сохранения"
grep -RFq '/v1/auth/token/revoke-self' "$OPENBAO_HOST_PACKAGE" \
    || die "Initial root token OpenBao должен отзываться после инициализации"
grep -Fq 'input_text=token' "$OPENBAO_HOST" \
    || die "Initial root token должен передаваться на отзыв только через stdin"
grep -RFq '"ssh-client-signer"' "$OPENBAO_HOST_PACKAGE" \
    || die "OpenBao должен иметь отдельный SSH-центр доступа"
grep -RFq '"ssh-host-signer"' "$OPENBAO_HOST_PACKAGE" \
    || die "OpenBao должен иметь отдельный SSH-центр серверов"
grep -Fq 'SSH_ACCESS_PATH = KEY_DIR / "ssh-access.json"' "$OPENBAO_HOST" \
    || die "Служебные SSH-доступы OpenBao должны храниться только в pve-only"
grep -Fq 'KV_ACCESS_PATH = KEY_DIR / "kv-access.json"' "$OPENBAO_HOST" \
    || die "Служебный доступ к KV должен храниться только в pve-only"
grep -Fq 'KV_MOUNT = "infra-secrets"' "$OPENBAO_HOST" \
    || die "OpenBao должен использовать отдельный mount рабочих секретов"
grep -RFq '"options": {"version": "2"}' "$OPENBAO_HOST_PACKAGE" \
    || die "Хранилище рабочих секретов должно быть KV v2"
grep -RFq '"pve/api/infra-manager"' "$OPENBAO_HOST_PACKAGE" \
    || die "KV должен содержать отдельный PVE API credential"
grep -RFq '"git/github/proxmox-read"' "$OPENBAO_HOST_PACKAGE" \
    || die "KV должен содержать отдельный Git credential"
grep -RFq '"services/semaphore"' "$OPENBAO_HOST_PACKAGE" \
    || die "KV должен содержать сервисные credentials Semaphore"
grep -RFq 'infra-manager-kv-read' "$OPENBAO_HOST_PACKAGE" \
    || die "KV должен иметь отдельную ограниченную read-policy"
grep -Fq '/run/infra-manager/secrets' "$OPENBAO_HOST" \
    || die "Секреты OpenBao должны материализоваться только во временную область"
grep -RFq 'infra-manager-ssh-ca-config' "$OPENBAO_HOST_PACKAGE" \
    || die "OpenBao должен иметь отдельную политику настройки SSH CA"
grep -RFq 'infra-manager-ssh-signer' "$OPENBAO_HOST_PACKAGE" \
    || die "OpenBao должен иметь отдельную политику подписи SSH"
grep -RFq '"token_bound_cidrs": ["127.0.0.1/32"]' "$OPENBAO_HOST_PACKAGE" \
    || die "Служебные OpenBao token должны быть ограничены loopback"
grep -RFq '/v1/sys/generate-root/attempt' "$OPENBAO_HOST_PACKAGE" \
    || die "OpenBao должен поддерживать штатный выпуск временного root token"
grep -Fq 'input_text=unseal_key' "$OPENBAO_HOST" \
    || die "Unseal-ключ для generate-root должен передаваться только через stdin"
grep -Fq '"push",' "$OPENBAO_HOST" \
    || die "Unseal-ключ должен передаваться в 910 временным файлом, не аргументом"
grep -Fq 'CT_KEY_PATH' "$OPENBAO_HOST" \
    || die "Временная копия unseal-ключа должна использовать каталог /run внутри 910"
grep -Fq '/usr/local/sbin/infra-manager-openbao-unseal' "$OPENBAO_STARTUP_COMMAND" \
    || die "910 должен вызывать хостовый сценарий OpenBao напрямую"
grep -Fq 'http://127.0.0.1:8200/v1/sys/seal-status' "$OPENBAO_STARTUP_COMMAND" \
    || die "Разблокировка при старте 910 должна ждать OpenBao"
grep -Fq 'ExecStart=/usr/local/sbin/infra-manager-openbao-startup-unseal {{ infra_pve_node }}' "$OPENBAO_STARTUP_SERVICE" \
    || die "Служба 910 должна запускать одноразовую разблокировку после старта"
grep -Fq 'WantedBy=multi-user.target' "$OPENBAO_STARTUP_SERVICE" \
    || die "Служба разблокировки должна запускаться вместе с 910"
grep -Fq 'name="Sync SSH Access"' "$PY_SEMAPHORE" \
    || die "Semaphore должен иметь задание Sync SSH Access"
grep -Fq 'sync_ssh_access(REPO_ROOT)' "$SSH_ACCESS_JOB" \
    || die "Задание должно синхронизировать OpenBao OTP"
grep -Fq 'sync_openbao_otp_contract(node, _otp_sources(repo_root))' "$PY_OPENBAO" \
    || die "Sync SSH Access должен только синхронизировать OTP-контракт OpenBao"
if grep -Fq 'run_deploy_guest' "$PY_OPENBAO"; then
    die "Sync SSH Access не должен развёртывать или перенастраивать гостей"
fi
if grep -Fq 'verify_ssh_access' "$PY_OPENBAO"; then
    die "Sync SSH Access не должен запускать приёмочную проверку"
fi
grep -Fq 'infra-openbao-ssh' "$SSH_ACCESS_ACCEPTANCE" \
    || die "Приёмочная проверка должна отдельно проверять реальный SSH OTP"
[[ ! -e "$ROOT/scripts/infra-manager/infra_manager/machine_ssh.py" ]] \
    || die "Старая реализация машинных SSH-сертификатов должна быть удалена"
[[ ! -e "$ROOT/scripts/infra-manager/infra_manager/ssh_access.py" ]] \
    || die "Приёмочная проверка не должна находиться в рабочем модуле infra_manager"
[[ ! -e "$ROOT/infrastructure/pve/systemd/infra-manager-openbao-unseal.service" ]] \
    || die "PVE не должен содержать постоянную systemd-службу OpenBao"
[[ ! -e "$ROOT/infrastructure/pve/systemd/infra-manager-openbao-unseal.timer" ]] \
    || die "PVE не должен содержать периодический timer OpenBao"
grep -q 'install_openbao_host_support' "$PY_OPENBAO" \
    || die "Задание OpenBao должно устанавливать хостовый сценарий"
grep -q 'initialize_openbao_on_host' "$PY_OPENBAO" \
    || die "Задание OpenBao должно выполнять первичную инициализацию через PVE"
if grep -q 'infra-manager@pve' "$ANSIBLE_PLAYBOOK" "$ANSIBLE_LINUX_BASE" "$ANSIBLE_GUEST_LAYOUT" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Старая PVE-идентичность не должна присутствовать в чистой схеме"
fi
if grep -q 'InfraManagedGuest' "$ANSIBLE_PLAYBOOK" "$ANSIBLE_LINUX_BASE" "$ANSIBLE_GUEST_LAYOUT" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Старая собственная роль PVE не должна присутствовать в чистой схеме"
fi
if grep -q 'PVE API automation\|Ansible managed guests' "$PY_SEMAPHORE"; then
    die "Неиспользуемые Semaphore credentials не должны создаваться"
fi

grep -q 'SEMAPHORE_DB_DIALECT=sqlite' "$ANSIBLE_RUNTIME" \
    || die "Semaphore должен использовать SQLite в первой версии"
grep -q 'SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite' "$ANSIBLE_RUNTIME" \
    || die "Не зафиксирован постоянный путь SQLite"
grep -q 'SEMAPHORE_USE_REMOTE_RUNNER' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен удалять старую настройку remote Runner"
grep -q 'SEMAPHORE_RUNNER_REGISTRATION_TOKEN' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен удалять старый registration token Runner"
grep -q 'provision.paths.semaphore_persistent_data' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен обслуживать физический каталог постоянного хранилища Semaphore"
grep -q 'provision.paths.opentofu_persistent_state_dir' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен заранее создавать физический каталог OpenTofu state"
if grep -q 'recurse: true' "$ANSIBLE_RUNTIME" && grep -q 'provision.paths.semaphore_data' "$ANSIBLE_RUNTIME"; then
    die "Ansible не должен применять recurse к символической ссылке Semaphore"
fi
grep -q '"1001"' "$ANSIBLE_RUNTIME" \
    || die "Хранилище Semaphore должно использовать uid 1001"
grep -q 'packer, version' "$ANSIBLE_RUNTIME" \
    || die "Packer должен проверяться внутри infra-runtime"
if grep -Fq 'register: ansible_private_key' "$ANSIBLE_RUNTIME"; then
    die "register ansible_private_key запрещён: это служебная переменная SSH connection plugin"
fi
grep -Fq 'register: infra_ansible_private_key_stat' "$ANSIBLE_RUNTIME" \
    || die "Проверка постоянного Ansible-ключа должна использовать безопасное имя переменной"

grep -q 'runtime-activation.log' "$ACTIVATE_RUNTIME" \
    || die "Команда активации должна вести отдельный журнал"
grep -Fq '.infra-manager-runtime-activation-pending' "$PY_COMMON" "$ACTIVATE_RUNTIME" \
    || die "Задания и активация runtime должны использовать общий маркер"
grep -Fq 'docker exec --user 0 infra-runtime' "$ACTIVATE_RUNTIME" \
    || die "Активация должна ждать завершения текущего самообновления infra-manager"
grep -Fq 'infra-manager-openbao-startup-unseal "$PVE_NODE"' "$ACTIVATE_RUNTIME" \
    || die "После перезапуска runtime OpenBao должен разблокироваться до проверки"
grep -Fq 'run_pve_openbao --check-kv --log-level quiet' "$OPENBAO_STARTUP_COMMAND" \
    || die "Startup unseal должен проверять KV после восстановления секретов"
grep -Fq 'run_pve_openbao --check-ssh-access --log-level quiet' "$OPENBAO_STARTUP_COMMAND" \
    || die "Startup unseal должен проверять SSH CA, ssh-otp и auth/machine"
grep -Fq -- '--cacert "$tls_ca"' "$OPENBAO_STARTUP_COMMAND" \
    || die "Startup unseal должен проверять TLS-вход OpenBao доверенным CA"
grep -Fq ':8202/v1/sys/health' "$OPENBAO_STARTUP_COMMAND" \
    || die "Startup unseal должен проверять внешний TLS listener :8202"
if grep -Fq 'echo "[ОК] OpenBao уже разблокирован"' "$OPENBAO_STARTUP_COMMAND"; then
    die "Startup unseal не должен завершаться до восстановления секретов и проверок"
fi
grep -Fq 'на PVE выполните infra-manager repair' "$OPENBAO_STARTUP_COMMAND" \
    || die "Startup unseal должен направлять оператора в единую PVE-команду"
grep -q 'infra-manager-status --full --quiet' "$ACTIVATE_RUNTIME" \
    || die "Отложенная активация должна завершаться полной проверкой infra-manager"
if grep -Fq 'infra-manager-status' "$ANSIBLE_VERIFY"; then
    die "Ansible verify не должен выполнять финальный status infra-manager до Initialize OpenBao"
fi

grep -q 'INFRA_PVE_HOST_DIR' "$PY_SETTINGS" \
    || die "Путь PVE SSH должен поддерживать отдельный bootstrap-runtime"
grep -Fq 'INFRA_PVE_HOST_DIR=/etc/bootstrap-runner/pve-host' "$ROOT/scripts/bootstrap-runner/run-runtime.sh" \
    || die "bootstrap-runtime должен использовать временный PVE SSH-каталог 990"
grep -q 'python_install_root: Path = Path("/usr/local/lib/infra-manager")' "$PY_SETTINGS" \
    || die "Постоянный путь установки Python package должен быть зафиксирован"
grep -Fq 'dest: "{{ provision.paths.python_package }}/infra_manager/"' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать служебный код infra_manager"
python_wrapper_count="$(grep -Fc 'source: python-command.sh' "$ANSIBLE_RUNTIME" || true)"
[[ "$python_wrapper_count" -eq 1 ]] \
    || die "Внутри 910 должна устанавливаться только техническая status-команда"
grep -Fq 'target: infra-manager-status' "$ANSIBLE_RUNTIME" \
    || die "Ansible не устанавливает внутреннюю status-команду"
for obsolete in infra-manager-pve-access-check infra-manager-pve-lifecycle-test; do
    grep -Fq "/usr/local/sbin/$obsolete" "$ANSIBLE_RUNTIME" \
        || die "Ansible должен удалять прежнюю операторскую команду $obsolete"
done
grep -Fq 'dest: /usr/bin/infra-manager-status' "$ANSIBLE_RUNTIME" \
    || die "Команда infra-manager-status должна быть доступна через pct exec"
grep -q 'activate-runtime.sh' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать команду активации infra-runtime"
grep -Fq '/run/infra-manager/secrets' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен готовить временную область рабочих секретов"
python3 - "$ANSIBLE_RUNTIME" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
directory_block = '''path: /run/infra-manager/secrets
    state: directory
    owner: root
    group: root
    mode: "0750"'''
if directory_block not in text:
    raise SystemExit("runtime secret directory must be root:root 0750")

targets = {
    "/run/infra-manager/secrets/pve-api.env": (
        'dest: "{{ provision.access_materialization.pve.runtime_credential }}"',
        "dest: /run/infra-manager/secrets/pve-api.env",
    ),
    "/run/infra-manager/secrets/github_proxmox_repo_ed25519": (
        'dest: "{{ provision.access_materialization.github.private_key }}"',
        "dest: /run/infra-manager/secrets/github_proxmox_repo_ed25519",
    ),
    "/run/infra-manager/secrets/semaphore-server.env": (
        "dest: /run/infra-manager/secrets/semaphore-server.env",
    ),
    "/run/infra-manager/secrets/initial-admin-password": (
        "dest: /run/infra-manager/secrets/initial-admin-password",
    ),
}
for target, markers in targets.items():
    starts = [text.find(marker) for marker in markers]
    starts = [start for start in starts if start >= 0]
    if not starts:
        raise SystemExit(f"missing runtime secret target: {target}")
    start = min(starts)
    block = text[start : start + 260]
    if 'owner: "1001"' not in block:
        raise SystemExit(f"{target} must belong to uid 1001")
    if 'group: "0"' not in block:
        raise SystemExit(f"{target} must use gid 0")
    if 'mode: "0600"' not in block:
        raise SystemExit(f"{target} must use mode 0600")
PY
grep -Fq '/usr/local/sbin/infra-manager-openbao-unseal' "$ANSIBLE_RUNTIME" \
    || die "Повторная настройка должна сначала восстанавливать секреты из OpenBao"
grep -q 'infra-manager ansible self' "$ANSIBLE_RUNTIME" \
    || die "910 должен сохранять управляемый блок собственного Ansible-ключа"
grep -q 'systemd-run' "$ANSIBLE_PLAYBOOK" \
    || die "Общий playbook должен передавать активацию внешней systemd-службе"
grep -Fq '"INFRA_PVE_NODE={{ infra_pve_node }}"' "$ANSIBLE_PLAYBOOK" \
    || die "Активация runtime должна знать узел PVE для разблокировки OpenBao"
if grep -Fq -- '--on-active=30s' "$ANSIBLE_PLAYBOOK"; then
    die "Фиксированная задержка 30 секунд не должна управлять активацией runtime"
fi
branch_env_count="$(grep -Fc 'INFRA_PROJECT_BRANCH: "{{ infra_project_branch' "$ANSIBLE_RUNTIME" || true)"
[[ "$branch_env_count" -ge 1 ]] \
    || die "Выбранная ветка должна передаваться настройке Semaphore"
grep -Fq 'f"INFRA_PROJECT_BRANCH={self.project_branch}"' "$BOOTSTRAP_INFRA" \
    || die "Финальная bootstrap-проверка 910 должна использовать выбранную ветку"
grep -Fq 'self.initialize_infra_openbao()' "$BOOTSTRAP_HOST" \
    || die "Bootstrap должен инициализировать OpenBao перед финальной проверкой 910"
grep -Fq 'self.verify_infra_ready(quiet=True)' "$BOOTSTRAP_HOST" \
    || die "Bootstrap должен завершаться финальным status --full после Initialize OpenBao"

