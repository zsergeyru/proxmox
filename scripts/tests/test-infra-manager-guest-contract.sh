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
PORTAL_GATEWAY_SERVICE="$ROOT/infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/infra-manager-portal-gateway.service"
OPENBAO_JOB="$ROOT/scripts/infra-manager/jobs/initialize-openbao.py"
SSH_ACCESS_JOB="$ROOT/scripts/infra-manager/jobs/sync-ssh-access.py"
SSH_ACCESS_ACCEPTANCE="$ROOT/scripts/acceptance/verify-ssh-access.py"
PY_ACCESS_POLICY="$ROOT/scripts/infra-manager/infra_manager/access.py"
PY_OPENBAO="$ROOT/scripts/infra-manager/infra_manager/openbao.py"
PY_RECOVERY="$ROOT/scripts/infra-manager/infra_manager/recovery.py"
PY_OPERATOR="$ROOT/scripts/infra-manager/infra_manager/operator.py"
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

for file in "$PY_COMMON" "$ANSIBLE_CONFIG" "$ANSIBLE_PLAYBOOK" "$ANSIBLE_LINUX_BASE" "$PROJECT_GIT_TASKS" "$ANSIBLE_GUEST_LAYOUT" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SETTINGS" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE" "$PY_LIFECYCLE" "$PYTHON_COMMAND" "$ACTIVATE_RUNTIME" "$BUILD_TEMPLATE" "$DEPLOY_GUEST" "$PY_OPENTOFU" "$PY_TEMPLATE" "$PY_TEMPLATE_BUILD" "$PY_TEMPLATE_VERIFY" "$COMPOSE" "$OPENBAO_CONFIG" "$OPENBAO_HOST" "$OPENBAO_STARTUP_COMMAND" "$OPENBAO_STARTUP_SERVICE" "$PORTAL_GATEWAY_SERVICE" "$OPENBAO_JOB" "$SSH_ACCESS_JOB" "$SSH_ACCESS_ACCEPTANCE" "$PY_ACCESS_POLICY" "$PY_OPENBAO" "$PY_RECOVERY" "$RECOVERY_HOST" "$DOCKERFILE" "$REQ" "$PLAN" "$PY_PVE_HOST" "$OPENTOFU_LOCK" "$GUEST_MANIFEST" "$PROVISION" "$SSH_CONFIG"; do
    [[ -s "$file" ]] || die "Отсутствует обязательный файл: $file"
done

grep -Fq 'src: "{{ guest_root }}/rootfs/etc/systemd/system/infra-manager-portal-gateway.service"' "$ANSIBLE_RUNTIME" \
    || die "Portal gateway unit должен устанавливаться из checkout текущего задания"
if grep -Fq 'bootstrap_repository }}/infrastructure/guests/{{ guest }}/rootfs/etc/systemd/system/infra-manager-portal-gateway.service' "$ANSIBLE_RUNTIME"; then
    die "Portal gateway unit не должен браться из старой bootstrap-repo"
fi

PVE_ACCESS_TASKS="$ANSIBLE_RUNTIME_DIR/pve_access.yml"
SEMAPHORE_TASKS="$ANSIBLE_RUNTIME_DIR/semaphore.yml"

runtime_secret_dir_line="$(grep -nF -- '- name: Подготовить временную область рабочих секретов' "$PVE_ACCESS_TASKS" | head -n1 | cut -d: -f1 || true)"
openbao_restore_line="$(grep -nF -- '- name: Восстановить рабочие секреты из OpenBao при повторной настройке' "$PVE_ACCESS_TASKS" | head -n1 | cut -d: -f1 || true)"
pve_runtime_check_line="$(grep -nF -- '- name: Проверить рабочий PVE API credential после OpenBao' "$PVE_ACCESS_TASKS" | head -n1 | cut -d: -f1 || true)"
pve_bootstrap_check_line="$(grep -nF -- '- name: Проверить временный bootstrap PVE API credential' "$PVE_ACCESS_TASKS" | head -n1 | cut -d: -f1 || true)"
pve_stage_line="$(grep -nF -- '- name: Подготовить PVE API credential первого запуска' "$PVE_ACCESS_TASKS" | head -n1 | cut -d: -f1 || true)"
pve_check_line="$(grep -nF -- '- name: Проверить рабочий доступ к PVE' "$PVE_ACCESS_TASKS" | head -n1 | cut -d: -f1 || true)"
[[ -n "$runtime_secret_dir_line" && -n "$openbao_restore_line" && -n "$pve_runtime_check_line" && -n "$pve_bootstrap_check_line" && -n "$pve_stage_line" && -n "$pve_check_line" ]] \
    || die "pve_access.yml должен сначала использовать OpenBao и только затем bootstrap fallback"
(( runtime_secret_dir_line < openbao_restore_line && openbao_restore_line < pve_runtime_check_line && pve_runtime_check_line < pve_bootstrap_check_line && pve_bootstrap_check_line < pve_stage_line && pve_stage_line < pve_check_line )) \
    || die "Нарушен порядок OpenBao -> bootstrap fallback -> итоговая проверка PVE API"
grep -Fq 'dest: "{{ provision.access_materialization.pve.runtime_credential }}"' "$PVE_ACCESS_TASKS" \
    || die "Bootstrap PVE API credential должен попадать только во временный runtime path"
if grep -Fq '/mnt/pve-access/pve-api' "$ANSIBLE_RUNTIME"; then
    die "Переходный access/pve-api не должен использоваться Ansible"
fi

RECOVERY_TASKS="$ANSIBLE_RUNTIME_DIR/recovery.yml"
grep -Fq 'recovery-prepare' "$RECOVERY_TASKS" \
    || die "Роль infra_manager должна готовить PVE-only recovery-контур"
grep -Fq 'INFRA_PVE_NODE: "{{ infra_pve_node }}"' "$RECOVERY_TASKS" \
    || die "Recovery должен использовать явный PVE node из Ansible"
grep -Fq 'RECOVERY_GITHUB_KEY = RECOVERY_DIR / "github_proxmox_repo_ed25519"' "$RECOVERY_HOST" \
    || die "PVE recovery helper должен иметь аварийную Git-копию"
grep -Fq -- '--restore-git-access' "$RECOVERY_HOST" \
    || die "PVE recovery helper должен уметь восстанавливать bootstrap Git-доступ"
[[ -s "$PVE_OPERATOR" ]] \
    || die "PVE должен иметь минимальную оболочку infra-manager"
[[ -s "$PY_OPERATOR" ]] \
    || die "Операторская логика должна находиться внутри infra-manager"
grep -Fq '"pct",' "$PVE_OPERATOR" \
    || die "PVE-оболочка должна передавать команды через pct exec"
grep -Fq '"--trusted-pve"' "$PVE_OPERATOR" \
    || die "PVE-оболочка должна явно отмечать доверенный терминальный вызов"
grep -Fq 'verify_manager(vmid, name)' "$PVE_OPERATOR" \
    || die "PVE-оболочка должна проверять VMID и имя управляющего LXC"
grep -Fq 'f"hostname: {name}"' "$PVE_OPERATOR" \
    || die "PVE-оболочка должна подтверждать hostname управляющего LXC"
grep -Fq 'RECOVERY_COMMAND' "$PVE_OPERATOR" \
    || die "PVE-оболочка должна отдельно передавать recover аварийному helper"
if grep -Eq 'docker|Semaphore|OPENBAO_COMMAND|ACTIVATE_RUNTIME|BOOTSTRAP_URL' "$PVE_OPERATOR"; then
    die "PVE-оболочка не должна содержать рабочую логику infra-manager"
fi
grep -Fq 'def operator_repair(' "$PY_OPERATOR" \
    || die "Обычный repair управляющего контура должен выполняться внутри 910"
grep -Fq 'repair_openbao_on_host(node)' "$PY_OPERATOR" \
    || die "Repair внутри 910 должен использовать минимальный PVE-only OpenBao helper"
grep -Fq 'ACTIVATE_RUNTIME' "$PY_OPERATOR" \
    || die "Repair внутри 910 должен активировать управляющую среду"
grep -Fq 'verify_recovery_state(require_approle=False)' "$RECOVERY_HOST" \
    || die "Recover должен начинаться с проверки PVE-only состояния"
grep -Fq 'restore_git_access()' "$RECOVERY_HOST" \
    || die "Recover должен восстанавливать bootstrap Git-доступ из PVE-only"
grep -Fq '["bash", str(bootstrap), "--recover"]' "$RECOVERY_HOST" \
    || die "PVE recovery helper должен запускать защищённый bootstrap recovery"
grep -Fq 'check_recovery_contour(node)' "$PY_OPENBAO" \
    || die "Внутренняя инициализация OpenBao должна завершаться полной recovery-проверкой"

grep -Fq 'roles_path = automation/ansible/roles' "$ANSIBLE_CONFIG" \
    || die "ansible.cfg должен задавать единый путь к roles"
grep -Fq 'ansible.builtin.include_tasks: project_git.yml' "$ANSIBLE_LINUX_BASE" \
    || die "linux_base должна подключать централизованную Git read-настройку"
grep -Fq 'path: /etc/proxmox-guest/credentials' "$PROJECT_GIT_TASKS" \
    || die "Git migration должна удалять старый credentials-каталог"
grep -Fq 'dest: /etc/proxmox-guest/git/github-proxmox-read' "$PROJECT_GIT_TASKS" \
    || die "Git read credential должен устанавливаться в новый каталог"
grep -Fq 'git@github-proxmox-read:zsergeyru/proxmox.git' "$PROJECT_GIT_TASKS" \
    || die "Git read-настройка должна быть ограничена проектным репозиторием"
grep -Fq 'git' "$PROJECT_GIT_TASKS" \
    || die "Git read-настройка должна проверять реальный доступ"
[[ ! -e "$ROOT/automation/ansible/tasks" ]] \
    || die "Старый каталог automation/ansible/tasks не должен возвращаться"

python3 - "$GUEST_MANIFEST" "$PROVISION" "$ANSIBLE_PLAYBOOK" "$ANSIBLE_LINUX_BASE" "$ANSIBLE_GUEST_LAYOUT" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SETTINGS" "$COMPOSE" "$DOCKERFILE" "$REQ" <<'PY'
from pathlib import Path
import sys
import yaml

guest_path, provision_path, playbook_path, linux_base_tasks_path, guest_layout_tasks_path, docker_tasks_path, runtime_tasks_path, settings_path, compose_path, dockerfile_path, req_path = map(Path, sys.argv[1:])

guest = yaml.safe_load(guest_path.read_text(encoding="utf-8"))
if guest.get("vmid") != 910 or guest.get("name") != "infra-manager":
    raise SystemExit("910 guest.yaml содержит неверный vmid/name")
if guest.get("role") != "infra-manager":
    raise SystemExit("Управляющий гость должен иметь role: infra-manager")
if guest.get("profile") != "debian-lxc-docker":
    raise SystemExit("910 должен использовать общий профиль debian-lxc-docker")
if guest.get("pve_management") is not False:
    raise SystemExit("910 не должен входить в собственный постоянный PVE-контур")
if guest.get("protection") is not True:
    raise SystemExit("910 должен иметь protection=true")
if guest.get("boot", {}).get("onboot") is not True:
    raise SystemExit("910 должен иметь onboot=true")
if guest.get("network", {}).get("ipv4") != "192.168.9.10":
    raise SystemExit("910 должен иметь адрес 192.168.9.10")
if guest.get("boot", {}).get("order") != 10:
    raise SystemExit("910 должен иметь порядок запуска 10")
if guest.get("boot", {}).get("startup_delay_seconds") != 30:
    raise SystemExit("910 должен иметь задержку запуска 30 секунд")
expected_resources = {
    "cores": 2,
    "memory_mb": 4096,
    "swap_mb": 1024,
    "disk_size_gb": 32,
    "disk_storage": "local-lvm",
}
if guest.get("resources") != expected_resources:
    raise SystemExit(f"неожиданные resources 910: {guest.get('resources')!r}")

provision = yaml.safe_load(provision_path.read_text(encoding="utf-8"))
if provision.get("schema_version") != 1 or provision.get("guest_vmid") != 910:
    raise SystemExit("provision.yaml 910 имеет неверную версию или guest_vmid")
if provision.get("boundaries", {}).get("opentofu_manages_self") is not False:
    raise SystemExit("infra-manager должен явно запрещать управление собственным объектом OpenTofu")
if "opentofu_manages_guest_910" in provision.get("boundaries", {}):
    raise SystemExit("Граница OpenTofu не должна содержать VMID в имени поля")
system = provision.get("system", {})
if system.get("distribution") != "debian" or system.get("version") != "13" or system.get("architecture") != "amd64":
    raise SystemExit("provision.yaml должен требовать Debian 13 amd64")

playbook_text = playbook_path.read_text(encoding="utf-8")
playbook_data = yaml.safe_load(playbook_text)
if not isinstance(playbook_data, list) or len(playbook_data) != 1:
    raise SystemExit("общий Ansible playbook должен содержать один сценарий")
play_tasks = playbook_data[0].get("tasks", [])
role_order = [
    task["ansible.builtin.include_role"].get("name")
    for task in play_tasks
    if isinstance(task, dict)
    and isinstance(task.get("ansible.builtin.include_role"), dict)
]
expected_role_order = ["linux_base", "docker", "infra_manager", "ai_control", "network_gateway", "portal", "guest_layout"]
if role_order != expected_role_order:
    raise SystemExit(
        f"неожиданный порядок Ansible roles: {role_order!r}; "
        f"ожидается {expected_role_order!r}"
    )

linux_base_tasks_text = linux_base_tasks_path.read_text(encoding="utf-8")
guest_layout_tasks_text = guest_layout_tasks_path.read_text(encoding="utf-8")
docker_tasks_text = docker_tasks_path.read_text(encoding="utf-8")
runtime_tasks_text = runtime_tasks_path.read_text(encoding="utf-8")
settings_text = settings_path.read_text(encoding="utf-8")

required_host_packages = set(system.get("required_packages", []))
expected_host_packages = {
    "ca-certificates", "curl", "git", "gnupg", "jq",
    "openssh-client", "openssl", "python3", "python3-yaml",
}
if required_host_packages != expected_host_packages:
    raise SystemExit(f"неожиданный список пакетов 910: {sorted(required_host_packages)}")
if 'name: linux_base' not in playbook_text:
    raise SystemExit("общий Ansible playbook не подключает роль linux_base")
if 'provision.system.required_packages' not in linux_base_tasks_text:
    raise SystemExit("роль linux_base не устанавливает system.required_packages")
if 'name: guest_layout' not in playbook_text:
    raise SystemExit("общий Ansible playbook не подключает роль guest_layout")
if 'provision.components' not in guest_layout_tasks_text:
    raise SystemExit("роль guest_layout не использует components из provision.yaml")

docker = provision.get("docker", {})
docker_packages = set(docker.get("required_packages", []))
expected_docker_packages = {
    "docker-ce", "docker-ce-cli", "containerd.io",
    "docker-buildx-plugin", "docker-compose-plugin",
}
if docker_packages != expected_docker_packages:
    raise SystemExit(f"неожиданные Docker-пакеты 910: {sorted(docker_packages)}")
if 'provision.docker.required_packages' not in docker_tasks_text:
    raise SystemExit("общий Ansible-модуль Docker не использует required_packages")

services = docker.get("services", {})
if set(services) != {"runtime", "openbao"}:
    raise SystemExit(
        "provision.yaml: ожидаются Docker services runtime и openbao: "
        f"{sorted(services)}"
    )
runtime = services["runtime"]
openbao = services["openbao"]
runtime_mounts = set(runtime.get("mounts", []))
required_runtime_mounts = {
    "/mnt/persistent-state/locks:/var/lib/infra-manager/locks",
    "/var/lib/infra-manager/bootstrap-repo:/var/lib/infra-manager/bootstrap-repo:ro",
}
if not required_runtime_mounts.issubset(runtime_mounts):
    raise SystemExit("provision.yaml не описывает mount блокировок и Git-копии runtime")
if runtime.get("container_name") != "infra-runtime":
    raise SystemExit("provision.yaml: container_name должен быть infra-runtime")
if runtime.get("image") != "infra-runtime:v1":
    raise SystemExit("provision.yaml: image должен быть infra-runtime:v1")
if runtime.get("base_image") != "semaphoreui/semaphore:v2.18.30":
    raise SystemExit("infra-runtime должен использовать Semaphore v2.18.30 как base image")
if runtime.get("network_mode") != "host":
    raise SystemExit("infra-runtime должен использовать network_mode=host для Packer HTTP")
if openbao.get("container_name") != "openbao":
    raise SystemExit("OpenBao должен использовать container_name=openbao")
if openbao.get("image") != "ghcr.io/openbao/openbao:2.7.0":
    raise SystemExit("OpenBao должен использовать закреплённую версию 2.7.0")
if openbao.get("network_mode") != "host":
    raise SystemExit("OpenBao должен использовать network_mode=host")
if openbao.get("api_address") != "http://127.0.0.1:8200":
    raise SystemExit("OpenBao API должен быть доступен только через loopback 910")
if openbao.get("data_path") != "/var/lib/persistent/openbao":
    raise SystemExit("OpenBao должен хранить данные в постоянном каталоге")
if openbao.get("storage") != "raft":
    raise SystemExit("OpenBao должен использовать Raft")
if openbao.get("auto_initialize") is not False:
    raise SystemExit("OpenBao не должен инициализироваться автоматически")
if provision.get("paths", {}).get("openbao_data") != "/var/lib/persistent/openbao":
    raise SystemExit("Путь постоянных данных OpenBao не зафиксирован")
if "/var/lib/persistent/openbao" not in provision.get("persistence", {}).get("backup_required", []):
    raise SystemExit("Данные OpenBao должны входить в обязательное резервное копирование")

target_layout = provision.get("persistence", {}).get("target_layout", {})
if target_layout.get("implemented") is not True:
    raise SystemExit("Штатная схема постоянного состояния должна быть включена")
if target_layout.get("host_root") != "/mnt/bindmounts/infra-manager":
    raise SystemExit("Не зафиксирован единый корень постоянных данных на PVE")
pve_only = target_layout.get("pve_only", {})
if pve_only.get("mounted_into_guest") is not False:
    raise SystemExit("PVE-only данные не должны монтироваться в 910")
if "openbao-unseal-key" not in pve_only.get("contains", []):
    raise SystemExit("Unseal-ключ OpenBao должен оставаться только на PVE")
access = target_layout.get("access", {})
if access.get("mode") != "ro" or access.get("guest_path") != "/mnt/pve-access":
    raise SystemExit("Данные доступа PVE должны подключаться в 910 только для чтения")
access_contains = set(access.get("contains", []))
for legacy in ("github-deploy-key", "pve-api-credential"):
    if legacy in access_contains:
        raise SystemExit(f"Переходный {legacy} не должен оставаться в access/")
required_access = {"pve-root-ssh-key", "pve-known-hosts", "pve-ca"}
if not required_access.issubset(access_contains):
    raise SystemExit("В access/ отсутствует постоянный несекретный/SSH набор 910")
pve_only = target_layout.get("pve_only", {})
if "recovery-github-deploy-key" not in set(pve_only.get("contains", [])):
    raise SystemExit("PVE-only должен содержать канонический recovery Git key")
state = target_layout.get("state", {})
if state.get("mode") != "rw" or state.get("guest_path") != "/mnt/persistent-state":
    raise SystemExit("Постоянное состояние 910 должно иметь отдельный rw mount")
required_state = {
    "openbao-raft",
    "opentofu-state",
    "semaphore-data",
    "ansible-identity",
    "operation-locks",
}
state_contains = set(state.get("contains", []))
if not required_state.issubset(state_contains):
    raise SystemExit("Целевая схема не содержит весь обязательный набор состояния 910")
if "infra-manager-secrets" in state_contains:
    raise SystemExit("Постоянный state/secrets не должен возвращаться")
guest_local = set(target_layout.get("guest_local", {}).get("contains", []))
if "git-checkout" not in guest_local:
    raise SystemExit("Git checkout должен оставаться локальным и воспроизводимым внутри 910")
bindings = state.get("bindings", [])
binding_pairs = {(item.get("source"), item.get("target")) for item in bindings}
required_bindings = {
    ("/mnt/persistent-state/ansible", "/etc/infra-manager/ansible"),
    ("/mnt/persistent-state/semaphore", "/var/lib/infra-manager/semaphore"),
    ("/mnt/persistent-state/opentofu", "/var/lib/infra-manager/opentofu"),
    ("/mnt/persistent-state/locks", "/var/lib/infra-manager/locks"),
    ("/mnt/persistent-state/openbao", "/var/lib/persistent/openbao"),
}
if not required_bindings.issubset(binding_pairs):
    raise SystemExit("Не зафиксированы все привязки постоянного состояния")
if any(
    "/mnt/persistent-state/secrets" in str(item)
    or "/etc/infra-manager/secrets" in str(item)
    for item in bindings
):
    raise SystemExit("Старое постоянное хранилище secrets не должно подключаться")
if "transition_sources" in provision:
    raise SystemExit("provision.yaml не должен содержать transition_sources")
if provision["access_materialization"]["pve"]["staging_credential"] != "/run/infra-manager/bootstrap-secrets/pve-api.env":
    raise SystemExit("Bootstrap PVE credential должен быть только временным")
if provision["access_materialization"]["github"]["staging_credential"] != "/run/infra-manager/bootstrap-secrets/github_proxmox_repo_ed25519":
    raise SystemExit("Bootstrap Git credential должен быть только временным")

if 'semaphore_version: str = "v2.18.30"' not in settings_text:
    raise SystemExit("Версия Semaphore в settings.py расходится с provision.yaml")
if 'runtime_version: str = "v1"' not in settings_text:
    raise SystemExit("Версия infra-runtime в settings.py расходится с provision.yaml")
if 'Path("/run/infra-manager/secrets")' not in settings_text:
    raise SystemExit("Рабочие секреты infra-manager должны находиться в /run")
if 'return self.runtime_secret_dir / "pve-api.env"' not in settings_text:
    raise SystemExit("PVE API consumer должен читать материализованный секрет OpenBao")
if 'return self.runtime_secret_dir / "github_proxmox_repo_ed25519"' not in settings_text:
    raise SystemExit("Git consumer должен читать материализованный секрет OpenBao")

tools = runtime.get("tools", {})
dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
opentofu_version = str(tools.get("opentofu", ""))
packer_version = str(tools.get("packer", ""))
if f"ARG OPENTOFU_VERSION={opentofu_version}" not in dockerfile_text:
    raise SystemExit("Версия OpenTofu в Dockerfile расходится с provision.yaml")
if f"ARG PACKER_VERSION={packer_version}" not in dockerfile_text:
    raise SystemExit("Версия Packer в Dockerfile расходится с provision.yaml")
for package in set(runtime.get("system_packages", [])):
    if package not in dockerfile_text:
        raise SystemExit(f"Dockerfile не устанавливает пакет infra-runtime из provision.yaml: {package}")
if "xorriso" in dockerfile_text:
    raise SystemExit("xorriso не нужен: preseed передаётся штатным HTTP-сервером Packer")
if f'opentofu_version: str = "{opentofu_version}"' not in settings_text:
    raise SystemExit("Версия OpenTofu в settings.py расходится с provision.yaml")
if f'packer_version: str = "{packer_version}"' not in settings_text:
    raise SystemExit("Версия Packer в settings.py расходится с provision.yaml")

req_text = req_path.read_text(encoding="utf-8")
for package, constraint in runtime.get("python_packages", {}).items():
    expected = f"{package}{constraint}"
    if expected not in req_text:
        raise SystemExit(f"requirements.txt расходится с provision.yaml: {expected}")

compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
compose_services = compose.get("services", {})
if set(compose_services) != {"runtime", "openbao"}:
    raise SystemExit(
        "docker-compose.yml: ожидаются services runtime и openbao: "
        f"{sorted(compose_services)}"
    )
if compose_services["runtime"].get("container_name") != runtime.get("container_name"):
    raise SystemExit("container_name infra-runtime расходится с provision.yaml")
if compose_services["runtime"].get("network_mode") != "host":
    raise SystemExit("infra-runtime Compose должен использовать network_mode=host")
if compose_services["runtime"].get("env_file") != [
    "/run/infra-manager/secrets/semaphore-server.env"
]:
    raise SystemExit("Semaphore должен получать рабочие секреты из временной области")
runtime_volumes = set(compose_services["runtime"].get("volumes", []))
if "/run/infra-manager/secrets:/run/infra-manager/secrets:ro" not in runtime_volumes:
    raise SystemExit("infra-runtime должен видеть материализованные секреты только для чтения")
if "/mnt/persistent-state/locks:/var/lib/infra-manager/locks" not in runtime_volumes:
    raise SystemExit("PVE CLI и Semaphore должны использовать общий каталог блокировок")
if "/var/lib/infra-manager/bootstrap-repo:/var/lib/infra-manager/bootstrap-repo:ro" not in runtime_volumes:
    raise SystemExit("infra-runtime должен читать постоянную рабочую Git-копию")

compose_openbao = compose_services["openbao"]
if compose_openbao.get("container_name") != openbao.get("container_name"):
    raise SystemExit("container_name OpenBao расходится с provision.yaml")
if compose_openbao.get("image") != "ghcr.io/openbao/openbao:${OPENBAO_VERSION}":
    raise SystemExit("Compose должен получать закреплённую версию OpenBao из .versions.env")
if compose_openbao.get("network_mode") != "host":
    raise SystemExit("OpenBao Compose должен использовать network_mode=host")
if compose_openbao.get("command") != ["server"]:
    raise SystemExit("OpenBao должен запускаться в обычном server-режиме, не dev")
if any("dev" in str(arg) for arg in compose_openbao.get("command", [])):
    raise SystemExit("OpenBao dev-режим запрещён")
required_openbao_volumes = {
    "/mnt/persistent-state/openbao:/openbao/file",
    "./openbao/openbao.hcl:/openbao/config/openbao.hcl:ro",
}
if not required_openbao_volumes.issubset(set(compose_openbao.get("volumes", []))):
    raise SystemExit("Compose не содержит обязательные тома OpenBao")

required_runtime_fragments = (
    "provision.access_materialization.pve.ca_source",
    "provision.access_materialization.pve.runtime_credential",
    "provision.paths.ansible_identity",
    "provision.paths.semaphore_persistent_data",
    "provision.paths.opentofu_persistent_state_dir",
    "provision.persistence.target_layout.state.bindings",
    "OPENBAO_VERSION",
    "openbao_seal_status",
    "openbao_unsealed_status",
    "infra_pve_node",
    "infra-manager-openbao-startup-unseal.service",
    "/usr/local/sbin/infra-manager-openbao-unseal",
    "render-opentofu-input.py",
    "--exclude-vmid",
    "docker-compose.yml",
    "semaphore-project",
    "infra-manager-status",
)
for fragment in required_runtime_fragments:
    if fragment not in runtime_tasks_text:
        raise SystemExit(f"Ansible infra-runtime не покрывает обязательный этап: {fragment}")
PY
[[ ! -e "$ROOT/scripts/bootstrap-runner/pve-access.sh" ]] \
    || die "Отдельный pve-access.sh 990 больше не должен существовать"
[[ ! -e "$ROOT/scripts/infra-manager/pve-bootstrap-access.sh" ]] \
    || die "Отдельный pve-bootstrap-access.sh 910 больше не должен существовать"
[[ ! -e "$ROOT/scripts/infra-manager/setup.sh" ]] \
    || die "Старый setup.sh не должен существовать"
[[ ! -e "$ROOT/scripts/infra-manager/infra_manager/setup.py" ]] \
    || die "Старый Python setup не должен существовать"
[[ ! -e "$ROOT/scripts/infra-manager/infra_manager/host_setup.py" ]] \
    || die "Старый HostSetup не должен существовать"
[[ ! -e "$ROOT/scripts/infra-manager/infra_manager/runtime_setup.py" ]] \
    || die "Старый RuntimeSetup не должен существовать"
[[ ! -e "$ROOT/scripts/infra-manager/infra_manager/setup_context.py" ]] \
    || die "Старый SetupContext не должен существовать"
if grep -q '"setup"' "$ROOT/scripts/infra-manager/infra_manager/cli.py"; then
    die "CLI не должен содержать отдельную команду setup"
fi
grep -q 'ansible.builtin.include_role:' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен использовать роли"
grep -q 'name: docker' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен подключать роль Docker"
grep -q 'name: infra_manager' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен подключать роль infra_manager"
grep -q 'name: linux_base' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен подключать роль linux_base"
grep -q 'provision.system.required_packages' "$ANSIBLE_LINUX_BASE" \
    || die "Роль linux_base должна устанавливать системные пакеты из provision.yaml"
grep -q 'name: guest_layout' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен подключать роль guest_layout"
grep -q 'provision.components' "$ANSIBLE_GUEST_LAYOUT" \
    || die "Роль guest_layout должна создавать каталоги по provision.yaml"

grep -q 'pve_host_private_key' "$PY_SETTINGS" \
    || die "Путь root SSH-ключа PVE должен находиться в единых настройках"
grep -q 'ensure_container_host' "$PY_PVE_HOST" \
    || die "Общий PVE host слой должен поддерживать container-host"
grep -q 'ensure_infra_self_access' "$PY_PVE_HOST" \
    || die "PVE host слой должен уметь безопасно восстановить самодоступ 910"
grep -Fq 'desired["keyctl"] = "1"' "$PY_PVE_HOST" \
    || die "container-host должен обеспечивать keyctl=1"
grep -Fq 'desired["nesting"] = "1"' "$PY_PVE_HOST" \
    || die "container-host должен обеспечивать nesting=1"
grep -q 'check_root_access' "$PY_PVE" \
    || die "Полная проверка PVE должна проверять root SSH"
grep -q 'ROOT_ADMIN_PRIVS = {' "$PY_PVE" \
    || die "PVE API token 910 должен проверяться как полный административный token"
grep -Fq '/mnt/pve-access/pve-host:/mnt/pve-access/pve-host:ro' "$COMPOSE" \
    || die "infra-runtime должен получать root SSH-доступ PVE из read-only каталога"
grep -q '^access_materialization:' "$PROVISION" \
    || die "provision 910 должен хранить только техническую материализацию доступов"
if grep -Eq '^[[:space:]]+(guest_scope|managed_pool_assignment|privilege_separation):' "$PROVISION"; then
    die "Политика доступа не должна дублироваться в provision.yaml"
fi
