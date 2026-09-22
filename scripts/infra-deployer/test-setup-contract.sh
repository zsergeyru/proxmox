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

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

for file in "$SETUP" "$STATUS" "$ACCESS" "$LIFECYCLE" "$COMPOSE" "$DOCKERFILE" "$REQ"; do
    [[ -s "$file" ]] || die "Отсутствует обязательный файл: $file"
done

grep -q 'SEMAPHORE_VERSION="v2.18.29"' "$SETUP" \
    || die "Semaphore должен быть зафиксирован на v2.18.29"

grep -q '^install_local_commands() {' "$SETUP" \
    || die "setup.sh обязан определять install_local_commands"

grep -q 'infra-deployer-status' "$SETUP" \
    || die "setup.sh не устанавливает status command"
grep -q 'infra-deployer-pve-access-check' "$SETUP" \
    || die "setup.sh не устанавливает PVE access check"
grep -q 'infra-deployer-pve-lifecycle-test' "$SETUP" \
    || die "setup.sh не устанавливает lifecycle test"

grep -q 'SEMAPHORE_DB_DIALECT=sqlite' "$SETUP" \
    || die "Semaphore должен использовать SQLite в первой версии"
grep -q 'SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite' "$SETUP" \
    || die "Не зафиксирован постоянный путь SQLite"

grep -q 'install -d -o 1001 -g 0' "$SETUP" \
    || die "Постоянные каталоги Semaphore/Runner должны быть доступны uid 1001"

grep -q '/etc/semaphore/requirements.txt' "$DOCKERFILE" \
    || die "Runner Dockerfile должен передавать Python requirements Semaphore runner"
grep -q '^proxmoxer' "$REQ" \
    || die "Runner должен содержать proxmoxer"

grep -q 'infra-deployer-status --full' "$STATUS" \
    || die "status.sh должен поддерживать отдельную полную проверку прав"
grep -q 'api2/json/version' "$STATUS" \
    || die "Базовый status должен реально проверять PVE API credential"

if grep -qE '(^|[[:space:]])pct create[[:space:]]+910|(^|[[:space:]])qm create[[:space:]]+910' "$SETUP"; then
    die "Приватный setup не должен создавать виртуальный объект 910"
fi

if grep -q '/etc/pve/' "$SETUP"; then
    die "setup внутри 910 не должен работать с файловой системой /etc/pve"
fi

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
    '/var/lib/infra-deployer/opentofu/state:/var/lib/infra-deployer/opentofu/state',
}
missing = required.difference(volumes)
if missing:
    raise SystemExit(f'missing runner volumes: {sorted(missing)}')

for volume in volumes:
    if 'pve-api.env' in volume:
        raise SystemExit('PVE API secret must not be bind-mounted directly into Runner')
PY

ok "Контракт setup infra-deployer проверен"
