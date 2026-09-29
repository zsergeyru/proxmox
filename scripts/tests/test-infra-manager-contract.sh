#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANSIBLE_PLAYBOOK="$ROOT/automation/ansible/playbooks/configure-guest.yml"
ANSIBLE_DOCKER="$ROOT/automation/ansible/tasks/configure-docker.yml"
ANSIBLE_RUNTIME="$ROOT/automation/ansible/tasks/configure-infra-runtime.yml"
PY_SETTINGS="$ROOT/scripts/infra-manager/infra_manager/settings.py"
PY_SEMAPHORE="$ROOT/scripts/infra-manager/infra_manager/semaphore.py"
PY_STATUS="$ROOT/scripts/infra-manager/infra_manager/status.py"
PY_PVE="$ROOT/scripts/infra-manager/infra_manager/pve.py"
STATUS="$ROOT/scripts/infra-manager/commands/status.sh"
ACCESS="$ROOT/scripts/infra-manager/commands/pve-access-check.sh"
LIFECYCLE="$ROOT/scripts/infra-manager/commands/pve-lifecycle-test.sh"
ACTIVATE_RUNTIME="$ROOT/scripts/infra-manager/commands/activate-runtime.sh"
BUILD_TEMPLATE="$ROOT/scripts/infra-manager/jobs/build-template.py"
VERIFY_TEMPLATE="$ROOT/scripts/infra-manager/jobs/verify-template.py"
DEPLOY_GUEST="$ROOT/scripts/infra-manager/jobs/deploy-guest.py"
PY_OPENTOFU="$ROOT/scripts/infra-manager/infra_manager/opentofu.py"
PY_TEMPLATE="$ROOT/scripts/infra-manager/infra_manager/template.py"
PY_TEMPLATE_BUILD="$ROOT/scripts/infra-manager/infra_manager/template_build.py"
PY_TEMPLATE_VERIFY="$ROOT/scripts/infra-manager/infra_manager/template_verify.py"
COMPOSE="$ROOT/infrastructure/guests/910-infra-manager/compose/docker-compose.yml"
OPENBAO_CONFIG="$ROOT/infrastructure/guests/910-infra-manager/compose/openbao/openbao.hcl"
DOCKERFILE="$ROOT/infrastructure/guests/910-infra-manager/compose/runtime/Dockerfile"
REQ="$ROOT/infrastructure/guests/910-infra-manager/compose/runtime/requirements.txt"
PLAN="$ROOT/scripts/infra-manager/jobs/opentofu-plan.py"
PY_PVE_HOST="$ROOT/scripts/infra-manager/infra_manager/pve_host.py"
OPENTOFU_LOCK="$ROOT/automation/opentofu/.terraform.lock.hcl"
GUEST_MANIFEST="$ROOT/infrastructure/guests/910-infra-manager/guest.yaml"
PROVISION="$ROOT/infrastructure/guests/910-infra-manager/provision.yaml"
SSH_CONFIG="$ROOT/infrastructure/guests/910-infra-manager/compose/runtime/ssh_config"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

for file in "$ANSIBLE_PLAYBOOK" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SETTINGS" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE" "$STATUS" "$ACCESS" "$LIFECYCLE" "$ACTIVATE_RUNTIME" "$BUILD_TEMPLATE" "$VERIFY_TEMPLATE" "$DEPLOY_GUEST" "$PY_OPENTOFU" "$PY_TEMPLATE" "$PY_TEMPLATE_BUILD" "$PY_TEMPLATE_VERIFY" "$COMPOSE" "$OPENBAO_CONFIG" "$DOCKERFILE" "$REQ" "$PLAN" "$PY_PVE_HOST" "$OPENTOFU_LOCK" "$GUEST_MANIFEST" "$PROVISION" "$SSH_CONFIG"; do
    [[ -s "$file" ]] || die "Отсутствует обязательный файл: $file"
done


python3 - "$GUEST_MANIFEST" "$PROVISION" "$ANSIBLE_PLAYBOOK" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SETTINGS" "$COMPOSE" "$DOCKERFILE" "$REQ" <<'PY'
from pathlib import Path
import sys
import yaml

guest_path, provision_path, playbook_path, docker_tasks_path, runtime_tasks_path, settings_path, compose_path, dockerfile_path, req_path = map(Path, sys.argv[1:])

guest = yaml.safe_load(guest_path.read_text(encoding="utf-8"))
if guest.get("vmid") != 910 or guest.get("name") != "infra-manager":
    raise SystemExit("910 guest.yaml содержит неверный vmid/name")
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
system = provision.get("system", {})
if system.get("distribution") != "debian" or system.get("version") != "13" or system.get("architecture") != "amd64":
    raise SystemExit("provision.yaml должен требовать Debian 13 amd64")

playbook_text = playbook_path.read_text(encoding="utf-8")
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
if 'provision.system.required_packages' not in playbook_text:
    raise SystemExit("общий Ansible playbook не устанавливает system.required_packages")

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
if 'semaphore_version: str = "v2.18.30"' not in settings_text:
    raise SystemExit("Версия Semaphore в settings.py расходится с provision.yaml")
if 'runtime_version: str = "v1"' not in settings_text:
    raise SystemExit("Версия infra-runtime в settings.py расходится с provision.yaml")

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
    "/var/lib/persistent/openbao:/openbao/file",
    "./openbao/openbao.hcl:/openbao/config/openbao.hcl:ro",
}
if not required_openbao_volumes.issubset(set(compose_openbao.get("volumes", []))):
    raise SystemExit("Compose не содержит обязательные тома OpenBao")

required_runtime_fragments = (
    "provision.access.pve.ca_source",
    "provision.access.pve.staging_credential",
    "provision.access.pve.persistent_credential",
    "provision.paths.ansible_identity",
    "provision.paths.semaphore_data",
    "provision.paths.openbao_data",
    "OPENBAO_VERSION",
    "openbao_seal_status",
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
grep -q 'configure-docker.yml' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен подключать настройку Docker"
grep -q 'configure-infra-runtime.yml' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен подключать настройку infra-runtime"
grep -q 'provision.system.required_packages' "$ANSIBLE_PLAYBOOK" \
    || die "Общий Ansible playbook должен устанавливать системные пакеты из provision.yaml"

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
grep -Fq '/etc/infra-manager/pve-host:/etc/infra-manager/pve-host:ro' "$COMPOSE" \
    || die "infra-runtime должен получать root SSH-доступ PVE только для чтения"
grep -q 'privilege_separation: false' "$PROVISION" \
    || die "Постоянный PVE API token должен использовать privsep=0"
grep -q 'permanent_root_ssh_to_pve: true' "$PROVISION" \
    || die "910 должен иметь зафиксированный root SSH-доступ к PVE"
grep -Fq 'path    = "/openbao/file/raft"' "$OPENBAO_CONFIG" \
    || die "OpenBao должен использовать постоянное Raft-хранилище"
grep -Fq 'address         = "127.0.0.1:8200"' "$OPENBAO_CONFIG" \
    || die "OpenBao API должен слушать только loopback 910"
grep -Fq 'tls_disable     = true' "$OPENBAO_CONFIG" \
    || die "Первая локальная версия OpenBao должна явно фиксировать локальный HTTP"
if grep -Eq '(^|[^a-z])dev([^a-z]|$)' "$OPENBAO_CONFIG"; then
    die "OpenBao config не должен содержать dev-режим"
fi
if grep -q 'infra-manager@pve' "$ANSIBLE_PLAYBOOK" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Старая PVE-идентичность не должна присутствовать в чистой схеме"
fi
if grep -q 'InfraManagedGuest' "$ANSIBLE_PLAYBOOK" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
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
grep -q 'provision.paths.semaphore_data' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен обслуживать постоянное хранилище Semaphore"
grep -q '"1001"' "$ANSIBLE_RUNTIME" \
    || die "Хранилище Semaphore должно использовать uid 1001"
grep -q 'packer, version' "$ANSIBLE_RUNTIME" \
    || die "Packer должен проверяться внутри infra-runtime"
if grep -Fq 'register: ansible_private_key' "$ANSIBLE_RUNTIME"; then
    die "register ansible_private_key запрещён: это служебная переменная SSH connection plugin"
fi
grep -Fq 'register: infra_ansible_private_key_stat' "$ANSIBLE_RUNTIME" \
    || die "Проверка постоянного Ansible-ключа должна использовать безопасное имя переменной"

grep -Fq 'exec python3 -m infra_manager status "$@"' "$STATUS" \
    || die "status.sh должен быть тонким Python wrapper"
grep -Fq 'exec python3 -m infra_manager pve-access-check "$@"' "$ACCESS" \
    || die "pve-access-check.sh должен быть тонким Python wrapper"
for wrapper in "$STATUS" "$ACCESS"; do
    grep -Fq '/usr/local/lib/infra-manager/infra_manager' "$wrapper" \
        || die "Установленный wrapper должен использовать постоянный Python package"
    grep -Fq '/var/lib/infra-manager/bootstrap-repo/scripts/infra-manager' "$wrapper" \
        || die "Wrapper должен сохранять canonical checkout как аварийный fallback"
done
grep -q 'runtime-activation.log' "$ACTIVATE_RUNTIME" \
    || die "Команда активации должна вести отдельный журнал"
grep -q 'infra-manager-status --full --quiet' "$ACTIVATE_RUNTIME" \
    || die "Отложенная активация должна завершаться полной проверкой 910"

grep -q 'python_install_root: Path = Path("/usr/local/lib/infra-manager")' "$PY_SETTINGS" \
    || die "Постоянный путь установки Python package должен быть зафиксирован"
grep -Fq 'dest: "{{ provision.paths.python_package }}/infra_manager/"' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать служебный код infra_manager"
grep -q 'status.sh' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать status wrapper"
grep -q 'pve-access-check.sh' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать PVE access wrapper"
grep -q 'pve-lifecycle-test.sh' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать lifecycle test"
grep -q 'activate-runtime.sh' "$ANSIBLE_RUNTIME" \
    || die "Ansible должен устанавливать команду активации infra-runtime"
grep -q 'infra-manager ansible self' "$ANSIBLE_RUNTIME" \
    || die "910 должен сохранять управляемый блок собственного Ansible-ключа"
grep -q 'systemd-run' "$ANSIBLE_RUNTIME" \
    || die "Самообновление 910 должно откладывать перезапуск infra-runtime"
branch_env_count="$(grep -Fc 'INFRA_PROJECT_BRANCH: "{{ infra_project_branch' "$ANSIBLE_RUNTIME" || true)"
[[ "$branch_env_count" -ge 2 ]] \
    || die "Выбранная ветка должна передаваться и настройке Semaphore, и финальной проверке 910"

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

grep -q 'ROOT_ADMIN_PRIVS = {' "$PY_PVE" \
    || die "Python PVE access check должен содержать административный контракт"
grep -Fq 'require_permissions(client, "/", ROOT_ADMIN_PRIVS)' "$PY_PVE" \
    || die "Полная проверка PVE access должна требовать права root@pam"
grep -q 'check_root_access(node)' "$PY_PVE" \
    || die "Полная проверка PVE access должна проверять root SSH"

if grep -q '/etc/pve/' "$ANSIBLE_PLAYBOOK" "$ANSIBLE_DOCKER" "$ANSIBLE_RUNTIME" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Настройка внутри 910 не должна работать с файловой системой /etc/pve"
fi

grep -q 'run_build_template' "$BUILD_TEMPLATE" \
    || die "Build Template должен передавать выполнение Python-модулю"
grep -q 'infra_manager.template_build import run_build_template' "$BUILD_TEMPLATE" \
    || die "Build Template должен использовать отдельный модуль сборки"
grep -q 'infra_manager.template_verify import run_verify_template' "$VERIFY_TEMPLATE" \
    || die "Verify Template должен использовать отдельный модуль проверки"
grep -q '^TEMPLATE_VERSION = 8' "$PY_TEMPLATE" \
    || die "Python-сборка должна работать с Template-Version 8"
grep -q 'run_verify_template(vmid)' "$PY_TEMPLATE_BUILD" \
    || die "После сборки должен запускаться короткий Full Clone test"
grep -q '^TEST_VMID = 9099' "$PY_TEMPLATE_VERIFY" \
    || die "Проверка шаблона 9000 должна использовать VMID 9099"
grep -q 'run_deploy_guest' "$DEPLOY_GUEST" \
    || die "Deploy Guest должен передавать выполнение Python-модулю"

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

grep -q '^OPENTOFU_ENV_NAME = SETTINGS.opentofu_env_name' "$PY_SEMAPHORE" \
    || die "Semaphore должен читать имя Variable Group из единых настроек"
grep -q 'opentofu_env_name: str = "OpenTofu PVE"' "$PY_SETTINGS" \
    || die "Имя Variable Group OpenTofu PVE должно быть зафиксировано"
grep -q 'TF_VAR_pve_endpoint' "$PY_SEMAPHORE" \
    || die "Variable Group должен передавать pve_endpoint"
grep -q 'TF_VAR_pve_api_token' "$PY_SEMAPHORE" \
    || die "Variable Group должен передавать pve_api_token"
grep -Fq 'secret_payload["operation"] = "update"' "$PY_SEMAPHORE" \
    || die "Существующий PVE token в Variable Group должен синхронизироваться"
grep -q '"override_secret": True' "$PY_SEMAPHORE" \
    || die "Существующий GitHub SSH key должен обновлять секретную часть"
grep -q 'name="OpenTofu Plan"' "$PY_SEMAPHORE" \
    || die "Semaphore должен создавать шаблон OpenTofu Plan"
grep -q 'scripts/infra-manager/jobs/opentofu-plan.py' "$PY_SEMAPHORE" \
    || die "OpenTofu Plan должен запускать отдельный Python-сценарий"
grep -q 'name="Build Template 9000"' "$PY_SEMAPHORE" \
    || die "Semaphore должен создавать задание Build Template 9000"
grep -q 'scripts/infra-manager/jobs/build-template.py' "$PY_SEMAPHORE" \
    || die "Build Template 9000 должен запускать отдельный Python-сценарий"
grep -Fq "arguments='[\"9000\"]'" "$PY_SEMAPHORE" \
    || die "Build Template 9000 должен иметь фиксированный VMID 9000"
grep -q 'scripts/infra-manager/jobs/deploy-guest.py' "$PY_SEMAPHORE" \
    || die "Deploy Guest 410 должен запускать универсальный Python-сценарий"
grep -Fq "arguments='[\"410\"]'" "$PY_SEMAPHORE" \
    || die "Deploy Guest 410 должен иметь фиксированный VMID 410"
grep -Fq 'name="Deploy Guest 910"' "$PY_SEMAPHORE" \
    || die "Semaphore должен создавать задание Deploy Guest 910"
grep -Fq "arguments='[\"910\"]'" "$PY_SEMAPHORE" \
    || die "Deploy Guest 910 должен иметь фиксированный VMID 910"
grep -q 'app: str = "python"' "$PY_SEMAPHORE" \
    || die "Semaphore infrastructure tasks должны по умолчанию выполняться как Python"
grep -q '"allow_override_args_in_task": False' "$PY_SEMAPHORE" \
    || die "Semaphore tasks не должны разрешать переопределение аргументов"
grep -q '"allow_override_branch_in_task": False' "$PY_SEMAPHORE" \
    || die "Semaphore tasks не должны разрешать переопределение Git-ветки"
grep -q '^def find_unique_by_name' "$PY_SEMAPHORE" \
    || die "Semaphore должен иметь общий поиск именованных объектов"
grep -q '^def require_unique_by_name' "$PY_SEMAPHORE" \
    || die "Semaphore status должен требовать единственный именованный объект"
grep -q '{"id": repository_id, \*\*payload}' "$PY_SEMAPHORE" \
    || die "PUT Git repository должен передавать repository id в теле"
grep -q '{"id": template_id, \*\*payload}' "$PY_SEMAPHORE" \
    || die "PUT Semaphore template должен передавать template id в теле"

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
    '/var/lib/infra-manager/opentofu:/var/lib/infra-manager/opentofu',
    '/etc/infra-manager/ca:/etc/infra-manager/ca:ro',
    '/etc/infra-manager/ansible:/etc/infra-manager/ansible:ro',
    '/etc/infra-manager/pve-host:/etc/infra-manager/pve-host:ro',
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

ok "Контракт setup infra-manager проверен"
