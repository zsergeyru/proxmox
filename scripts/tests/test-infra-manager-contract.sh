#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP="$ROOT/scripts/infra-manager/setup.sh"
PY_SETUP="$ROOT/scripts/infra-manager/infra_manager/setup.py"
PY_SETTINGS="$ROOT/scripts/infra-manager/infra_manager/settings.py"
PY_SEMAPHORE="$ROOT/scripts/infra-manager/infra_manager/semaphore.py"
PY_STATUS="$ROOT/scripts/infra-manager/infra_manager/status.py"
PY_PVE="$ROOT/scripts/infra-manager/infra_manager/pve.py"
STATUS="$ROOT/scripts/infra-manager/commands/status.sh"
ACCESS="$ROOT/scripts/infra-manager/commands/pve-access-check.sh"
LIFECYCLE="$ROOT/scripts/infra-manager/commands/pve-lifecycle-test.sh"
BUILD_TEMPLATE="$ROOT/scripts/infra-manager/jobs/build-template.py"
VERIFY_TEMPLATE="$ROOT/scripts/infra-manager/jobs/verify-template.py"
DEPLOY_GUEST="$ROOT/scripts/infra-manager/jobs/deploy-guest.py"
PY_OPENTOFU="$ROOT/scripts/infra-manager/infra_manager/opentofu.py"
PY_TEMPLATE="$ROOT/scripts/infra-manager/infra_manager/template.py"
COMPOSE="$ROOT/infrastructure/guests/910-infra-manager/compose/docker-compose.yml"
DOCKERFILE="$ROOT/infrastructure/guests/910-infra-manager/compose/runtime/Dockerfile"
REQ="$ROOT/infrastructure/guests/910-infra-manager/compose/runtime/requirements.txt"
PLAN="$ROOT/scripts/infra-manager/jobs/opentofu-plan.py"
PVE_BOOTSTRAP_ACCESS="$ROOT/scripts/infra-manager/pve-bootstrap-access.sh"
OPENTOFU_LOCK="$ROOT/automation/opentofu/.terraform.lock.hcl"
GUEST_MANIFEST="$ROOT/infrastructure/guests/910-infra-manager/guest.yaml"
PROVISION="$ROOT/infrastructure/guests/910-infra-manager/provision.yaml"
SSH_CONFIG="$ROOT/infrastructure/guests/910-infra-manager/compose/runtime/ssh_config"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

for file in "$SETUP" "$PY_SETUP" "$PY_SETTINGS" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE" "$STATUS" "$ACCESS" "$LIFECYCLE" "$BUILD_TEMPLATE" "$VERIFY_TEMPLATE" "$DEPLOY_GUEST" "$PY_OPENTOFU" "$PY_TEMPLATE" "$COMPOSE" "$DOCKERFILE" "$REQ" "$PLAN" "$PVE_BOOTSTRAP_ACCESS" "$OPENTOFU_LOCK" "$GUEST_MANIFEST" "$PROVISION" "$SSH_CONFIG"; do
    [[ -s "$file" ]] || die "Отсутствует обязательный файл: $file"
done


python3 - "$GUEST_MANIFEST" "$PROVISION" "$SETUP" "$PY_SETUP" "$PY_SETTINGS" "$COMPOSE" "$DOCKERFILE" "$REQ" <<'PY'
from pathlib import Path
import re
import sys
import yaml

guest_path, provision_path, setup_path, py_setup_path, settings_path, compose_path, dockerfile_path, req_path = map(Path, sys.argv[1:])

guest = yaml.safe_load(guest_path.read_text(encoding="utf-8"))
if guest.get("vmid") != 910 or guest.get("name") != "infra-manager":
    raise SystemExit("910 guest.yaml содержит неверный vmid/name")
if "profile" in guest:
    raise SystemExit("910 guest.yaml не должен содержать profile и попадать в OpenTofu")
if guest.get("protection") is not True:
    raise SystemExit("910 должен иметь protection=true")
if guest.get("boot", {}).get("onboot") is not True:
    raise SystemExit("910 должен иметь onboot=true")
expected_resources = {
    "cores": 2,
    "memory_mb": 2048,
    "swap_mb": 512,
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

setup_text = setup_path.read_text(encoding="utf-8")
python_setup_text = py_setup_path.read_text(encoding="utf-8")
settings_text = settings_path.read_text(encoding="utf-8")
required_host_packages = set(system.get("required_packages", []))
expected_host_packages = {
    "ca-certificates", "curl", "git", "gnupg", "jq",
    "openssh-client", "openssl", "python3", "python3-yaml",
}
if required_host_packages != expected_host_packages:
    raise SystemExit(f"неожиданный список пакетов 910: {sorted(required_host_packages)}")
for package in required_host_packages:
    if package not in python_setup_text:
        raise SystemExit(f"setup.sh не обеспечивает пакет из provision.yaml: {package}")

docker = provision.get("docker", {})
docker_packages = set(docker.get("required_packages", []))
expected_docker_packages = {
    "docker-ce", "docker-ce-cli", "containerd.io",
    "docker-buildx-plugin", "docker-compose-plugin",
}
if docker_packages != expected_docker_packages:
    raise SystemExit(f"неожиданные Docker-пакеты 910: {sorted(docker_packages)}")
for package in docker_packages:
    if package not in python_setup_text:
        raise SystemExit(f"setup.sh не обеспечивает Docker-пакет из provision.yaml: {package}")

services = docker.get("services", {})
if set(services) != {"runtime"}:
    raise SystemExit(f"provision.yaml: должен быть один Docker service runtime: {sorted(services)}")

runtime = services["runtime"]
if runtime.get("container_name") != "infra-runtime":
    raise SystemExit("provision.yaml: container_name должен быть infra-runtime")
if runtime.get("image") != "infra-runtime:v1":
    raise SystemExit("provision.yaml: image должен быть infra-runtime:v1")
base_image = runtime.get("base_image", "")
if base_image != "semaphoreui/semaphore:v2.18.30":
    raise SystemExit("infra-runtime должен использовать Semaphore v2.18.30 как base image")
if runtime.get("network_mode") != "host":
    raise SystemExit("infra-runtime должен использовать network_mode=host для Packer HTTP")
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
system_packages = set(runtime.get("system_packages", []))
for package in system_packages:
    if package not in dockerfile_text:
        raise SystemExit(f"Dockerfile не устанавливает пакет infra-runtime из provision.yaml: {package}")
if "xorriso" in system_packages or "xorriso" in dockerfile_text:
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
if set(compose_services) != {"runtime"}:
    raise SystemExit(f"docker-compose.yml: должен быть один service runtime: {sorted(compose_services)}")
if compose_services["runtime"].get("container_name") != runtime.get("container_name"):
    raise SystemExit("container_name infra-runtime расходится с provision.yaml")
if compose_services["runtime"].get("network_mode") != "host":
    raise SystemExit("infra-runtime Compose должен использовать network_mode=host")
PY

grep -Fq 'exec python3 -m infra_manager setup "$@"' "$SETUP" \
    || die "setup.sh должен передавать настройку Python CLI"
grep -Fq 'if ! command -v python3' "$SETUP" \
    || die "setup.sh должен bootstrap только сам Python на минимальном Debian"
if grep -q 'docker compose\|SEMAPHORE_DB_DIALECT\|repair_semaphore_storage' "$SETUP"; then
    die "setup.sh должен оставаться тонким Python wrapper"
fi

if grep -Eq '^[[:space:]]*IdentitiesOnly[[:space:]]+yes[[:space:]]*$' "$SSH_CONFIG"; then
    die "Semaphore Git использует временный ssh-agent; IdentitiesOnly yes блокирует Deploy Key"
fi

grep -q '^class Setup:' "$PY_SETUP" \
    || die "Python setup должен содержать единый Setup orchestration"
grep -q '^SEMAPHORE_VERSION = SETTINGS.semaphore_version' "$PY_SETUP" \
    || die "setup должен читать версию Semaphore из единых настроек"
grep -q '^RUNTIME_VERSION = SETTINGS.runtime_version' "$PY_SETUP" \
    || die "setup должен читать версию infra-runtime из единых настроек"
grep -q '^OPENTOFU_VERSION = SETTINGS.opentofu_version' "$PY_SETUP" \
    || die "setup должен читать версию OpenTofu из единых настроек"
grep -q '^PACKER_VERSION = SETTINGS.packer_version' "$PY_SETUP" \
    || die "setup должен читать версию Packer из единых настроек"

for method in prepare_directories ensure_base_packages install_docker copy_compose_assets seed_semaphore_known_hosts generate_ca_bundle persist_pve_api_secret ensure_semaphore_secrets prepare_opentofu_input write_runtime_versions deploy_semaphore wait_semaphore verify_semaphore_tools configure_semaphore_project install_local_commands; do
    grep -q "    def ${method}(" "$PY_SETUP" \
        || die "Python setup не содержит обязательный этап ${method}"
done

grep -q 'render-opentofu-input.py' "$PY_SETUP" \
    || die "Python setup должен генерировать OpenTofu input"
grep -q 'Используется существующий постоянный PVE API credential' "$PY_SETUP" \
    || die "Повторное обновление 910 должно работать без staging PVE secret"
grep -q 'from .semaphore import configure_project' "$PY_SETUP" \
    || die "Python setup должен напрямую использовать Semaphore-модуль"
grep -Fq 'configure_project(self.project_branch)' "$PY_SETUP" \
    || die "Python setup должен передавать текущую Git-ветку в Semaphore"
[[ ! -e "$ROOT/scripts/infra-manager/semaphore-project.sh" ]] \
    || die "Устаревший semaphore-project.sh больше не должен существовать"
grep -q 'status.sh' "$PY_SETUP" \
    || die "setup должен устанавливать совместимый status wrapper"
grep -q 'pve-access-check.sh' "$PY_SETUP" \
    || die "setup должен устанавливать совместимый PVE access wrapper"
grep -q 'pve-lifecycle-test.sh' "$PY_SETUP" \
    || die "На этапе 2 должен использоваться существующий lifecycle test"

grep -q 'API_USER="root@pam"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "PVE bootstrap access должен использовать существующий root@pam"
grep -q 'API_TOKEN_NAME="infra-manager"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "PVE bootstrap access должен создавать отдельный infra-manager token"
grep -q -- '--privsep 1' "$PVE_BOOTSTRAP_ACCESS" \
    || die "PVE API token должен использовать privsep=1"
grep -q '^rollback_new_api_token() {' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Новый PVE API token должен откатываться при ошибке передачи secret"
grep -q '^remove_token_acls() {' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Recovery должен уметь удалить ACL старого PVE API token до ротации"
grep -Fq 'remove_token_acls' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Recovery должен вызывать очистку ACL перед удалением token"
grep -Fq 'pveum acl delete "$path"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Очистка recovery должна удалять только ACL infra-manager token"
grep -q 'rm -f -- "$tmp"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Временный PVE API secret должен удаляться и при ошибке передачи"
for role in PVEAuditor PVEVMAdmin PVEDatastoreUser PVEDatastoreAdmin PVESDNUser; do
    grep -q "\"$role\"" "$PVE_BOOTSTRAP_ACCESS" \
        || die "В PVE bootstrap access отсутствует штатная роль $role"
done

grep -Fq 'ensure_token_acl "/vms" "PVEVMAdmin"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "910 должен получать PVEVMAdmin на /vms"
grep -Fq 'ensure_token_roles "/pool/$MANAGED_POOL" "PVEVMAdmin,PVEPoolUser"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "910 должен получать PVEVMAdmin и PVEPoolUser на managed"
grep -Fq 'ensure_token_acl "/storage/$ISO_STORAGE" "PVEDatastoreAdmin"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "910 должен иметь доступ к ISO storage для Packer"
if grep -q 'pveum user add.*infra-manager@pve' "$PVE_BOOTSTRAP_ACCESS"; then
    die "Отдельный пользователь infra-manager@pve больше не должен создаваться"
fi
if grep -q 'pveum role add.*InfraManagedGuest' "$PVE_BOOTSTRAP_ACCESS"; then
    die "Собственная роль InfraManagedGuest больше не должна создаваться"
fi
if grep -q 'infra-manager@pve' "$PVE_BOOTSTRAP_ACCESS" "$SETUP" "$PY_SETUP" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Старая PVE-идентичность не должна присутствовать в чистой схеме"
fi
if grep -q 'InfraManagedGuest' "$PVE_BOOTSTRAP_ACCESS" "$SETUP" "$PY_SETUP" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "Старая собственная роль PVE не должна присутствовать в чистой схеме"
fi
if grep -q 'PVE API automation\|Ansible managed guests' "$PY_SEMAPHORE"; then
    die "Неиспользуемые Semaphore credentials не должны создаваться"
fi

grep -q 'SEMAPHORE_DB_DIALECT=sqlite' "$PY_SETUP" \
    || die "Semaphore должен использовать SQLite в первой версии"
grep -q 'SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite' "$PY_SETUP" \
    || die "Не зафиксирован постоянный путь SQLite"
grep -q '^ADMIN_PASSWORD_SHOWN_FILE = ' "$PY_SETUP" \
    || die "Python setup должен хранить отметку однократного показа пароля Semaphore"
grep -q 'Пароль: {password}' "$PY_SETUP" \
    || die "Первичный пароль Semaphore должен один раз выводиться в терминал"
grep -Fq '"SEMAPHORE_USE_REMOTE_RUNNER="' "$PY_SETUP" \
    || die "Python setup должен удалять старую настройку remote Runner"
grep -Fq '"SEMAPHORE_RUNNER_REGISTRATION_TOKEN="' "$PY_SETUP" \
    || die "Python setup должен удалять старый registration token Runner"
grep -q 'line.startswith(' "$PY_SETUP" \
    || die "Удаление старых настроек Runner должно выполняться по началу строки"
grep -q 'def repair_semaphore_storage' "$PY_SETUP" \
    || die "Python setup обязан проверять права постоянного хранилища Semaphore"
grep -q '"1001:0"' "$PY_SETUP" \
    || die "Проверка хранилища Semaphore должна использовать штатный uid 1001"
grep -q '"packer", "version"' "$PY_SETUP" \
    || die "Packer должен проверяться внутри infra-runtime"

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
grep -q '^PYTHON_INSTALL_ROOT = PATHS.python_install_root' "$PY_SETUP" \
    || die "Python setup должен читать путь установки package из единых путей"
grep -q 'python_install_root: Path = Path("/usr/local/lib/infra-manager")' "$PY_SETTINGS" \
    || die "Постоянный путь установки Python package должен быть зафиксирован"
grep -q 'shutil.copytree(source_package, target_package)' "$PY_SETUP" \
    || die "Python setup должен синхронизировать установленный infra_manager package"

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

grep -q 'VM_ADMIN_PRIVS = {' "$PY_PVE" \
    || die "Python PVE access check должен содержать контракт VM privileges"
grep -Fq 'require_permissions(client, "/vms", VM_ADMIN_PRIVS)' "$PY_PVE" \
    || die "Полная проверка PVE access должна требовать управление всеми VM/LXC через /vms"
grep -q 'f"/pool/{MANAGED_POOL}"' "$PY_PVE" \
    || die "Полная проверка PVE access должна проверять managed pool"
grep -q '"/storage/local"' "$PY_PVE" \
    || die "Полная проверка PVE access должна проверять права Packer на ISO storage"
grep -q 'forbid_permissions(client, "/", FORBIDDEN_ROOT_PRIVS)' "$PY_PVE" \
    || die "Полная проверка PVE access должна запрещать административные root privileges"

if grep -qE '(^|[[:space:]])pct create[[:space:]]+910|(^|[[:space:]])qm create[[:space:]]+910' "$SETUP" "$PY_SETUP"; then
    die "Приватный setup не должен создавать виртуальный объект 910"
fi

if grep -q '/etc/pve/' "$SETUP" "$PY_SETUP" "$PY_SEMAPHORE" "$PY_STATUS" "$PY_PVE"; then
    die "setup внутри 910 не должен работать с файловой системой /etc/pve"
fi

grep -q 'run_build_template' "$BUILD_TEMPLATE" \
    || die "Build Template должен передавать выполнение Python-модулю"
grep -q '^TEMPLATE_VERSION = 8' "$PY_TEMPLATE" \
    || die "Python-сборка должна работать с Template-Version 8"
grep -q 'run_verify_template(vmid)' "$PY_TEMPLATE" \
    || die "После сборки должен запускаться короткий Full Clone test"
grep -q '^TEST_VMID = 9099' "$PY_TEMPLATE" \
    || die "Проверка шаблона 9000 должна использовать VMID 9099"
grep -q 'run_deploy_guest' "$DEPLOY_GUEST" \
    || die "Deploy Guest должен передавать выполнение Python-модулю"

grep -q 'run_plan' "$PLAN" \
    || die "OpenTofu Plan должен передавать выполнение Python-модулю"
grep -q '"-detailed-exitcode"' "$PY_OPENTOFU" \
    || die "OpenTofu Plan должен различать наличие изменений"
grep -q '"-lockfile=readonly"' "$PY_OPENTOFU" \
    || die "OpenTofu должен использовать только зафиксированный lock file"
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
grep -q 'app: str = "python"' "$PY_SEMAPHORE" \
    || die "Semaphore infrastructure tasks должны по умолчанию выполняться как Python"
grep -q '"allow_override_args_in_task": False' "$PY_SEMAPHORE" \
    || die "Semaphore tasks не должны разрешать переопределение аргументов"
grep -q '"allow_override_branch_in_task": False' "$PY_SEMAPHORE" \
    || die "Semaphore tasks не должны разрешать переопределение Git-ветки"
grep -q 'def unique_by_name' "$PY_SEMAPHORE" \
    || die "Semaphore setup должен явно останавливать настройку при дубликатах"
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
if set(services) != {'runtime'}:
    raise SystemExit(f'unexpected services: {sorted(services)}')

runtime = services['runtime']

if runtime.get('image') != 'infra-runtime:${RUNTIME_VERSION}':
    raise SystemExit('infra-runtime image must use pinned runtime version variable')
if runtime.get('container_name') != 'infra-runtime':
    raise SystemExit('infra-runtime must use the canonical container name')
if runtime.get('network_mode') != 'host':
    raise SystemExit('infra-runtime must use host network')

volumes = runtime.get('volumes', [])
required = {
    '/var/lib/infra-manager/opentofu:/var/lib/infra-manager/opentofu',
    '/etc/infra-manager/ca:/etc/infra-manager/ca:ro',
    '/etc/infra-manager/ansible:/etc/infra-manager/ansible:ro',
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
