#!/usr/bin/env bash
set -Eeuo pipefail

SEMAPHORE_URL="http://127.0.0.1:3000"
PROJECT_NAME="Proxmox Infrastructure"
PROJECT_ID_FILE="/var/lib/infra-deployer/semaphore/project-id"

SECRET_DIR="/etc/infra-deployer/secrets"
ADMIN_PASSWORD_FILE="${SECRET_DIR}/initial-admin-password"
PVE_API_ENV="${SECRET_DIR}/pve-api.env"
GITHUB_KEY="/root/.ssh/github_proxmox_repo_ed25519"
GITHUB_KEY_COPY="${SECRET_DIR}/github_project_ed25519"
ANSIBLE_KEY="${SECRET_DIR}/ansible_ed25519"
PUBLIC_KEY_DIR="/var/lib/infra-deployer/public-keys"
ANSIBLE_PUBLIC_KEY="${PUBLIC_KEY_DIR}/ansible_ed25519.pub"

PROJECT_REPO="git@github.com:zsergeyru/proxmox.git"
PROJECT_BRANCH="${INFRA_PROJECT_BRANCH:-infra-iac-redesign}"
RECOVER="${INFRA_DEPLOYER_RECOVER:-0}"

COOKIE=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

cleanup() {
    [[ -z "$COOKIE" ]] || rm -f "$COOKIE"
}
trap cleanup EXIT

read_env_value() {
    local key=$1 file=$2
    sed -n "s/^${key}=//p" "$file" | head -n1
}

api() {
    local method=$1 path=$2
    shift 2
    curl -fsS -b "$COOKIE" -X "$method" \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        "$@" "${SEMAPHORE_URL}/api${path}"
}

login() {
    local password payload
    [[ -s "$ADMIN_PASSWORD_FILE" ]] || die "Не найден первичный пароль Semaphore"
    password="$(cat "$ADMIN_PASSWORD_FILE")"
    COOKIE="$(mktemp /tmp/semaphore-cookie.XXXXXX)"
    chmod 0600 "$COOKIE"

    payload="$(jq -n --arg auth admin --arg password "$password"         '{auth:$auth,password:$password}')"

    curl -fsS -c "$COOKIE" -X POST \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        -d "$payload" \
        "${SEMAPHORE_URL}/api/auth/login" >/dev/null \
        || die "Не удалось войти в Semaphore API"
}

ensure_project() {
    local projects id payload response
    projects="$(api GET /projects)"
    id="$(jq -r --arg name "$PROJECT_NAME"         '.[] | select(.name == $name) | .id' <<<"$projects" | head -n1)"

    if [[ -z "$id" ]]; then
        payload="$(jq -n --arg name "$PROJECT_NAME"             '{name:$name,alert:false,max_parallel_tasks:1,demo:false}')"
        response="$(api POST /projects -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
        [[ -n "$id" ]] || die "Semaphore создал проект, но не вернул id"
    fi

    install -d -o root -g root -m 0750 "$(dirname "$PROJECT_ID_FILE")"
    printf '%s\n' "$id" >"$PROJECT_ID_FILE"
    chmod 0640 "$PROJECT_ID_FILE"
    printf '%s' "$id"
}

key_id_by_name() {
    local project_id=$1 name=$2
    api GET "/project/${project_id}/keys?sort=name&order=asc" \
        | jq -r --arg name "$name" '.[] | select(.name == $name) | .id' \
        | head -n1
}

ensure_ssh_key() {
    local project_id=$1 name=$2 login_name=$3 private_key_file=$4
    local id private_key payload response

    id="$(key_id_by_name "$project_id" "$name")"
    [[ -z "$id" ]] || { printf '%s' "$id"; return; }

    private_key="$(cat "$private_key_file")"
    payload="$(jq -n \
        --arg name "$name" \
        --arg login "$login_name" \
        --arg private_key "$private_key" \
        --argjson project_id "$project_id" \
        '{name:$name,type:"ssh",project_id:$project_id,ssh:{login:$login,passphrase:"",private_key:$private_key}}')"

    response="$(api POST "/project/${project_id}/keys" -d "$payload")"
    id="$(jq -r '.id // empty' <<<"$response")"
    [[ -n "$id" ]] || die "Не удалось создать SSH key '$name' в Semaphore"
    printf '%s' "$id"
}

ensure_pve_key() {
    local project_id=$1
    local name="PVE API automation"
    local id token_id token_secret payload response

    token_id="$(read_env_value PVE_API_TOKEN_ID "$PVE_API_ENV")"
    token_secret="$(read_env_value PVE_API_TOKEN_SECRET "$PVE_API_ENV")"
    [[ -n "$token_id" && -n "$token_secret" ]] || die "PVE API credential неполон"

    id="$(key_id_by_name "$project_id" "$name")"

    payload="$(jq -n \
        --arg name "$name" \
        --arg login "$token_id" \
        --arg password "$token_secret" \
        --argjson project_id "$project_id" \
        '{name:$name,type:"login_password",project_id:$project_id,override_secret:true,login_password:{login:$login,password:$password}}')"

    if [[ -z "$id" ]]; then
        response="$(api POST "/project/${project_id}/keys" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
        [[ -n "$id" ]] || die "Не удалось создать PVE API key в Semaphore"
    elif [[ "$RECOVER" == "1" ]]; then
        api PUT "/project/${project_id}/keys/${id}" -d "$payload" >/dev/null
    fi

    printf '%s' "$id"
}

persist_github_key() {
    [[ -s "$GITHUB_KEY" ]] || die "GitHub Deploy Key не найден: $GITHUB_KEY"

    if [[ -f "$GITHUB_KEY_COPY" ]]; then
        cmp -s "$GITHUB_KEY" "$GITHUB_KEY_COPY" \
            || {
                [[ "$RECOVER" == "1" ]] || die "GitHub key изменился вне recovery"
                install -o root -g root -m 0600 "$GITHUB_KEY" "$GITHUB_KEY_COPY"
            }
    else
        install -o root -g root -m 0600 "$GITHUB_KEY" "$GITHUB_KEY_COPY"
    fi
}

ensure_ansible_key() {
    install -d -o root -g root -m 0700 "$SECRET_DIR"
    install -d -o root -g root -m 0755 "$PUBLIC_KEY_DIR"

    if [[ ! -f "$ANSIBLE_KEY" ]]; then
        [[ ! -e "${ANSIBLE_KEY}.pub" ]] || die "Есть Ansible public key без private key"
        ssh-keygen -q -t ed25519 -N '' -C infra-deployer-ansible -f "$ANSIBLE_KEY"
    fi

    chmod 0600 "$ANSIBLE_KEY"
    install -o root -g root -m 0644 "${ANSIBLE_KEY}.pub" "$ANSIBLE_PUBLIC_KEY"
}

ensure_repository() {
    local project_id=$1 ssh_key_id=$2
    local repos id payload

    repos="$(api GET "/project/${project_id}/repositories?sort=name&order=asc")"
    id="$(jq -r '.[] | select(.name == "proxmox") | .id' <<<"$repos" | head -n1)"

    payload="$(jq -n \
        --arg name proxmox \
        --arg git_url "$PROJECT_REPO" \
        --arg git_branch "$PROJECT_BRANCH" \
        --argjson project_id "$project_id" \
        --argjson ssh_key_id "$ssh_key_id" \
        '{name:$name,project_id:$project_id,git_url:$git_url,git_branch:$git_branch,ssh_key_id:$ssh_key_id}')"

    if [[ -z "$id" ]]; then
        api POST "/project/${project_id}/repositories" -d "$payload" >/dev/null
    else
        api PUT "/project/${project_id}/repositories/${id}" -d "$payload" >/dev/null
    fi
}

main() {
    local project_id github_key_id pve_key_id ansible_key_id

    command -v jq >/dev/null 2>&1 || die "Не найден jq"
    command -v curl >/dev/null 2>&1 || die "Не найден curl"

    [[ -s "$PVE_API_ENV" ]] || die "Не найден постоянный PVE API credential"

    persist_github_key
    ensure_ansible_key
    login

    project_id="$(ensure_project)"
    github_key_id="$(ensure_ssh_key "$project_id" "GitHub project read-only" git "$GITHUB_KEY_COPY")"
    pve_key_id="$(ensure_pve_key "$project_id")"
    ansible_key_id="$(ensure_ssh_key "$project_id" "Ansible managed guests" root "$ANSIBLE_KEY")"
    ensure_repository "$project_id" "$github_key_id"

    [[ -n "$pve_key_id" && -n "$ansible_key_id" ]] || die "Не все ключи Semaphore созданы"
    ok "Проект Semaphore, Key Store и Git repository подготовлены"
}

main "$@"
