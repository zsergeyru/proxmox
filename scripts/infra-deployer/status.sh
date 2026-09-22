#!/usr/bin/env bash
set -Eeuo pipefail

FULL=0
PVE_ENV="/etc/infra-deployer/secrets/pve-api.env"
CA_BUNDLE="/etc/infra-deployer/ca/ca-bundle.crt"
SEMAPHORE_URL="http://127.0.0.1:3000"
SEMAPHORE_API_TOKEN_FILE="/etc/infra-deployer/secrets/semaphore-api-token"
PROJECT_ID_FILE="/var/lib/infra-deployer/semaphore-project-id"

C_RESET=""
C_BOLD=""
C_GREEN=""
C_RED=""

if [[ "${INFRA_DEPLOYER_COLOR:-0}" == "1" && "${NO_COLOR:-}" == "" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
fi

die() { printf '%s%sОШИБКА:%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
ok()  { printf '%s%s[ОК]%s %s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$*"; }

usage() {
    cat <<'USAGE'
Использование:
  infra-deployer-status
  infra-deployer-status --full

Без параметров проверяется готовность самого 910 и базовая авторизация PVE API.
--full дополнительно проверяет окончательный контракт прав OpenTofu.
USAGE
}

if (($#)); then
    case "$1" in
        --full)
            [[ $# -eq 1 ]] || { usage >&2; exit 2; }
            FULL=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
fi

required_file() {
    [[ -s "$1" ]] || die "Отсутствует обязательный файл: $1"
}

read_env_value() {
    local key=$1
    sed -n "s/^${key}=//p" "$PVE_ENV" | head -n1
}

semaphore_api_get() {
    local path=$1 token
    token="$(cat "$SEMAPHORE_API_TOKEN_FILE")"
    curl -fsS         --connect-timeout 2         --max-time 10         -H "Authorization: Bearer $token"         -H 'Accept: application/json'         "${SEMAPHORE_URL}/api${path}"
}

command -v docker >/dev/null 2>&1 || die "Docker не установлен"
docker compose version >/dev/null 2>&1 || die "Docker Compose недоступен"

required_file "$PVE_ENV"
required_file /etc/infra-deployer/secrets/semaphore-server.env
required_file /etc/infra-deployer/secrets/semaphore-runner.env
required_file "$SEMAPHORE_API_TOKEN_FILE"
required_file /etc/infra-deployer/secrets/github_project_ed25519
required_file "$PROJECT_ID_FILE"
required_file /var/lib/infra-deployer/opentofu/guests.json
required_file "$CA_BUNDLE"

[[ -d /var/lib/infra-deployer/opentofu/state ]] \
    || die "Отсутствует каталог OpenTofu state"

[[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-semaphore 2>/dev/null || true)" == "true" ]] \
    || die "Semaphore Server не запущен"
[[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-runner 2>/dev/null || true)" == "true" ]] \
    || die "Semaphore Runner не запущен"

curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3000/api/ping >/dev/null \
    || die "Semaphore API не отвечает"


PROJECT_ID="$(cat "$PROJECT_ID_FILE")"
[[ "$PROJECT_ID" =~ ^[0-9]+$ ]] || die "Некорректный project-id Semaphore"

semaphore_api_get "/project/${PROJECT_ID}/environment?sort=name&order=asc"     | jq -e '.[] | select(.name == "OpenTofu PVE")' >/dev/null     || die "В Semaphore отсутствует Variable Group OpenTofu PVE"

semaphore_api_get "/project/${PROJECT_ID}/templates?sort=name&order=asc"     | jq -e '.[] | select(.name == "OpenTofu Plan" and .app == "bash")' >/dev/null     || die "В Semaphore отсутствует шаблон OpenTofu Plan"

docker exec infra-deployer-runner tofu version >/dev/null \
    || die "OpenTofu недоступен"
docker exec infra-deployer-runner packer version >/dev/null \
    || die "Packer недоступен"
docker exec infra-deployer-runner ansible --version >/dev/null \
    || die "Ansible недоступен"
docker exec infra-deployer-runner python3 -c 'import proxmoxer' >/dev/null \
    || die "proxmoxer недоступен"

PVE_API_URL="$(read_env_value PVE_API_URL)"
PVE_API_TOKEN_ID="$(read_env_value PVE_API_TOKEN_ID)"
PVE_API_TOKEN_SECRET="$(read_env_value PVE_API_TOKEN_SECRET)"
[[ -n "$PVE_API_URL" && -n "$PVE_API_TOKEN_ID" && -n "$PVE_API_TOKEN_SECRET" ]] \
    || die "PVE credential неполон"

curl -fsS \
    --connect-timeout 5 \
    --max-time 20 \
    --cacert "$CA_BUNDLE" \
    -H "Authorization: PVEAPIToken=${PVE_API_TOKEN_ID}=${PVE_API_TOKEN_SECRET}" \
    "${PVE_API_URL}/api2/json/version" >/dev/null \
    || die "PVE API credential не проходит базовую авторизацию"

unset PVE_API_TOKEN_SECRET

if ((FULL == 1)); then
    [[ -x /usr/local/sbin/infra-deployer-pve-access-check ]] \
        || die "Отсутствует infra-deployer-pve-access-check"
    /usr/local/sbin/infra-deployer-pve-access-check >/dev/null \
        || die "PVE API access не соответствует полному контракту"
    ok "infra-deployer готов, полный контракт PVE API подтверждён"
else
    ok "infra-deployer готов"
fi
