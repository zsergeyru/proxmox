#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP="$ROOT/scripts/infra-deployer/setup.sh"
STATUS="$ROOT/scripts/infra-deployer/status.sh"
ACCESS="$ROOT/scripts/infra-deployer/check-pve-access.sh"
LIFECYCLE="$ROOT/scripts/infra-deployer/test-pve-lifecycle.sh"
COMPOSE="$ROOT/guests/910-infra-deployer/compose/docker-compose.yml"
DOCKERFILE="$ROOT/guests/910-infra-deployer/compose/runner/Dockerfile"
REQ="$ROOT/guests/910-infra-deployer/compose/runner/requirements.txt"
PLAN="$ROOT/scripts/infra-deployer/opentofu-plan.sh"
SEMAPHORE_PROJECT="$ROOT/scripts/infra-deployer/semaphore-project.sh"
PVE_BOOTSTRAP_ACCESS="$ROOT/scripts/infra-deployer/pve-bootstrap-access.sh"
OPENTOFU_LOCK="$ROOT/opentofu/.terraform.lock.hcl"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

for file in "$SETUP" "$STATUS" "$ACCESS" "$LIFECYCLE" "$COMPOSE" "$DOCKERFILE" "$REQ" "$PLAN" "$SEMAPHORE_PROJECT" "$PVE_BOOTSTRAP_ACCESS" "$OPENTOFU_LOCK"; do
    [[ -s "$file" ]] || die "Отсутствует обязательный файл: $file"
done

grep -q 'SEMAPHORE_VERSION="v2.18.30"' "$SETUP" \
    || die "Semaphore должен быть зафиксирован на v2.18.30"

grep -q '^install_local_commands() {' "$SETUP" \
    || die "setup.sh обязан определять install_local_commands"

grep -q 'infra-deployer-status' "$SETUP" \
    || die "setup.sh не устанавливает status command"
grep -q 'infra-deployer-pve-access-check' "$SETUP" \
    || die "setup.sh не устанавливает PVE access check"
grep -q 'infra-deployer-pve-lifecycle-test' "$SETUP" \
    || die "setup.sh не устанавливает lifecycle test"

grep -q 'ln -sfn "$STATUS_COMMAND" /usr/local/bin/infra-deployer-status' "$SETUP" \
    || die "status command должен быть доступен по короткому имени через pct exec"
grep -q 'ln -sfn "$ACCESS_CHECK_COMMAND" /usr/local/bin/infra-deployer-pve-access-check' "$SETUP" \
    || die "PVE access check должен быть доступен по короткому имени через pct exec"
grep -q 'ln -sfn "$LIFECYCLE_TEST_COMMAND" /usr/local/bin/infra-deployer-pve-lifecycle-test' "$SETUP" \
    || die "lifecycle test должен быть доступен по короткому имени через pct exec"

grep -q 'render-opentofu-input.py' "$SETUP" \
    || die "setup.sh должен генерировать OpenTofu input"

grep -q 'Используется существующий постоянный PVE API credential' "$SETUP" \
    || die "Повторное обновление 910 должно работать без staging PVE secret"

grep -q 'API_USER="root@pam"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "PVE bootstrap access должен использовать существующий root@pam"
grep -q 'API_TOKEN_NAME="infra-deployer"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "PVE bootstrap access должен создавать отдельный infra-deployer token"
grep -q -- '--privsep 1' "$PVE_BOOTSTRAP_ACCESS" \
    || die "PVE API token должен использовать privsep=1"
grep -q '^rollback_new_api_token() {' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Новый PVE API token должен откатываться при ошибке передачи secret"
grep -q 'rm -f -- "$tmp"' "$PVE_BOOTSTRAP_ACCESS" \
    || die "Временный PVE API secret должен удаляться и при ошибке передачи"
for role in PVEAuditor PVEVMAdmin PVEDatastoreUser PVESDNUser; do
    grep -q "\"$role\"" "$PVE_BOOTSTRAP_ACCESS" \
        || die "В PVE bootstrap access отсутствует штатная роль $role"
done
if grep -q 'pveum user add.*infra-deployer@pve' "$PVE_BOOTSTRAP_ACCESS"; then
    die "Отдельный пользователь infra-deployer@pve больше не должен создаваться"
fi
if grep -q 'pveum role add.*InfraManagedGuest' "$PVE_BOOTSTRAP_ACCESS"; then
    die "Собственная роль InfraManagedGuest больше не должна создаваться"
fi
if grep -q 'infra-deployer@pve' "$PVE_BOOTSTRAP_ACCESS" "$SETUP" "$SEMAPHORE_PROJECT"; then
    die "Старая PVE-идентичность не должна присутствовать в чистой схеме"
fi
if grep -q 'InfraManagedGuest' "$PVE_BOOTSTRAP_ACCESS" "$SETUP" "$SEMAPHORE_PROJECT"; then
    die "Старая собственная роль PVE не должна присутствовать в чистой схеме"
fi
if grep -q 'PVE API automation\|Ansible managed guests' "$SEMAPHORE_PROJECT"; then
    die "Неиспользуемые Semaphore credentials не должны создаваться"
fi

grep -q 'SEMAPHORE_DB_DIALECT=sqlite' "$SETUP" \
    || die "Semaphore должен использовать SQLite в первой версии"
grep -q 'SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite' "$SETUP" \
    || die "Не зафиксирован постоянный путь SQLite"

grep -q 'install -d -o 1001 -g 0' "$SETUP" \
    || die "Постоянные каталоги Semaphore/Runner должны быть доступны uid 1001"

grep -q '^repair_semaphore_storage() {' "$SETUP" \
    || die "setup.sh обязан проверять права постоянного хранилища Semaphore через Docker"
grep -q -- '--user 1001:0' "$SETUP" \
    || die "Проверка хранилища Semaphore должна выполняться от штатного uid 1001"
grep -q 'repair_semaphore_storage' "$SETUP" \
    || die "Восстановление прав SQLite должно вызываться при запуске Semaphore"

grep -q 'PROJECT_ID_FILE="/var/lib/infra-deployer/semaphore-project-id"' "$SEMAPHORE_PROJECT" \
    || die "Semaphore project-id должен храниться вне каталога SQLite"
grep -q 'PROJECT_ID_FILE="/var/lib/infra-deployer/semaphore-project-id"' "$STATUS" \
    || die "status.sh должен читать Semaphore project-id из постоянного служебного пути"
if grep -q '/var/lib/infra-deployer/semaphore/project-id' "$SEMAPHORE_PROJECT" "$STATUS"; then
    die "project-id запрещено хранить внутри каталога SQLite Semaphore"
fi
if grep -qE 'install -d .*\$\(dirname "\$PROJECT_ID_FILE"\)' "$SEMAPHORE_PROJECT"; then
    die "semaphore-project.sh не должен менять права родительского каталога project-id"
fi

grep -q '/etc/semaphore/requirements.txt' "$DOCKERFILE" \
    || die "Runner Dockerfile должен передавать Python requirements Semaphore runner"
grep -q '^proxmoxer' "$REQ" \
    || die "Runner должен содержать proxmoxer"

grep -q 'infra-deployer-status --full' "$STATUS" \
    || die "status.sh должен поддерживать отдельную полную проверку прав"
grep -q 'api2/json/version' "$STATUS" \
    || die "Базовый status должен реально проверять PVE API credential"
grep -q 'GitHub project read-only' "$STATUS" \
    || die "status.sh должен проверять GitHub SSH key Semaphore"
grep -q 'PROJECT_REPO="git@github.com:zsergeyru/proxmox.git"' "$STATUS" \
    || die "status.sh должен проверять Git repository Semaphore"
grep -q '^forbid_unmanaged_guest_mutation() {' "$ACCESS" \
    || die "Полная проверка PVE access должна запрещать изменения вне managed"
grep -q '/cluster/resources?type=vm' "$ACCESS" \
    || die "Проверка PVE access должна просматривать все существующие VM/LXC"

if grep -qE '(^|[[:space:]])pct create[[:space:]]+910|(^|[[:space:]])qm create[[:space:]]+910' "$SETUP"; then
    die "Приватный setup не должен создавать виртуальный объект 910"
fi

if grep -q '/etc/pve/' "$SETUP"; then
    die "setup внутри 910 не должен работать с файловой системой /etc/pve"
fi

if grep -qE 'tofu[[:space:]].*(apply|destroy)' "$PLAN"; then
    die "OpenTofu Plan не должен содержать apply или destroy"
fi

grep -q 'tofu -chdir="$OPENTOFU_DIR" plan' "$PLAN"     || die "OpenTofu Plan должен выполнять tofu plan"
grep -q -- '-lockfile=readonly' "$PLAN" \
    || die "OpenTofu Plan должен использовать только зафиксированный lock file"
grep -q 'provider "registry.opentofu.org/bpg/proxmox"' "$OPENTOFU_LOCK" \
    || die "OpenTofu lock file должен фиксировать bpg/proxmox из OpenTofu Registry"
grep -q 'version     = "0.112.0"' "$OPENTOFU_LOCK" \
    || die "OpenTofu lock file должен фиксировать bpg/proxmox 0.112.0"

grep -q 'OPENTOFU_ENV_NAME="OpenTofu PVE"' "$SEMAPHORE_PROJECT" \
    || die "Semaphore должен создавать Variable Group OpenTofu PVE"
grep -q 'TF_VAR_pve_endpoint' "$SEMAPHORE_PROJECT" \
    || die "Variable Group должен передавать pve_endpoint"
grep -q 'TF_VAR_pve_api_token' "$SEMAPHORE_PROJECT" \
    || die "Variable Group должен передавать pve_api_token"
grep -q 'operation:"update"' "$SEMAPHORE_PROJECT" \
    || die "Существующий PVE token в Variable Group должен синхронизироваться при обычном обновлении"
grep -q 'local name="OpenTofu Plan"' "$SEMAPHORE_PROJECT" \
    || die "Semaphore должен создавать шаблон OpenTofu Plan"
grep -q 'opentofu_plan_template_id="$(ensure_opentofu_plan_template ' "$SEMAPHORE_PROJECT" \
    || die "main semaphore-project.sh обязан вызывать создание шаблона OpenTofu Plan"
grep -q 'scripts/infra-deployer/opentofu-plan.sh' "$SEMAPHORE_PROJECT" \
    || die "OpenTofu Plan должен запускать отдельный безопасный сценарий"

if grep -q '^ensure_pve_key() {' "$SEMAPHORE_PROJECT"; then
    die "Отдельный PVE credential в Semaphore Key Store больше не нужен"
fi
if grep -q '^ensure_ansible_key() {' "$SEMAPHORE_PROJECT"; then
    die "Ansible SSH credential не должен создаваться до появления Ansible-задач"
fi
grep -q 'allow_override_args_in_task:false' "$SEMAPHORE_PROJECT" \
    || die "OpenTofu Plan не должен разрешать переопределение аргументов"
grep -q 'allow_override_branch_in_task:false' "$SEMAPHORE_PROJECT" \
    || die "OpenTofu Plan не должен разрешать переопределение Git-ветки"

grep -q '^shopt -s inherit_errexit$' "$SEMAPHORE_PROJECT" \
    || die "Ошибки API внутри командных подстановок должны останавливать semaphore-project.sh"
grep -q '^unique_id_by_name() {' "$SEMAPHORE_PROJECT" \
    || die "Semaphore setup должен останавливать настройку при дубликатах объектов"
grep -Fq 'unique_id_by_name "$repos" "proxmox" "Git repository"' "$SEMAPHORE_PROJECT" \
    || die "Git repository Semaphore должен проверяться на дубликаты"
grep -q "'{id:\$id,name:\$name,project_id:\$project_id,git_url:\$git_url" "$SEMAPHORE_PROJECT" \
    || die "PUT Git repository должен передавать repository id в теле"
grep -q 'id:\$id,' "$SEMAPHORE_PROJECT" \
    || die "PUT OpenTofu Plan должен передавать template id в теле"

python3 - "$COMPOSE" <<'PY'
from pathlib import Path
import sys, yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text(encoding='utf-8'))
services = data.get('services', {})
if set(services) != {'semaphore', 'runner'}:
    raise SystemExit(f'unexpected services: {sorted(services)}')

server = services['semaphore']
runner = services['runner']

if server.get('image') != 'semaphoreui/semaphore:${SEMAPHORE_VERSION}':
    raise SystemExit('Semaphore image must use pinned version variable')

if runner.get('container_name') != 'infra-deployer-runner':
    raise SystemExit('unexpected runner container name')

volumes = runner.get('volumes', [])
required = {
    '/var/lib/infra-deployer/opentofu:/var/lib/infra-deployer/opentofu',
}
missing = required.difference(volumes)
if missing:
    raise SystemExit(f'missing runner volumes: {sorted(missing)}')

for volume in volumes:
    if 'pve-api.env' in volume:
        raise SystemExit('PVE API secret must not be bind-mounted directly into Runner')
    if 'public-keys' in volume:
        raise SystemExit('Unused Ansible public-key volume must not be mounted into Runner')
PY

ok "Контракт setup infra-deployer проверен"
