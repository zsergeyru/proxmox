#!/usr/bin/env bash
set -Eeuo pipefail

SEMAPHORE_URL="http://127.0.0.1:3000"
PROJECT_NAME="Proxmox Infrastructure"
OPENTOFU_ENV_NAME="OpenTofu PVE"
PROJECT_ID_FILE="/var/lib/infra-deployer/semaphore/project-id"

SECRET_DIR="/etc/infra-deployer/secrets"
ADMIN_PASSWORD_FILE="${SECRET_DIR}/initial-admin-password"
SEMAPHORE_API_TOKEN_FILE="${SECRET_DIR}/semaphore-api-token"
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
AUTH_MODE="cookie"

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

api_cookie() {
    local method=$1 path=$2
    shift 2
    curl -fsS -b "$COOKIE" -X "$method" \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        "$@" "${SEMAPHORE_URL}/api${path}"
}

api_token() {
    local method=$1 path=$2 token
    shift 2
    token="$(cat "$SEMAPHORE_API_TOKEN_FILE")"
    curl -fsS -X "$method" \
        -H "Authorization: Bearer $token" \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        "$@" "${SEMAPHORE_URL}/api${path}"
}

api() {
    if [[ "$AUTH_MODE" == "token" ]]; then
        api_token "$@"
    else
        api_cookie "$@"
    fi
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
    AUTH_MODE="cookie"
}

ensure_api_token() {
    local payload response token

    if [[ -s "$SEMAPHORE_API_TOKEN_FILE" ]]; then
        AUTH_MODE="token"
        if api_token GET /user/ >/dev/null 2>&1; then
            return
        fi
        AUTH_MODE="cookie"
    fi

    login
    payload='{"name":"infra-deployer setup"}'
    response="$(api_cookie POST /user/tokens -d "$payload")"
    token="$(jq -r '.id // empty' <<<"$response")"
    [[ -n "$token" ]] || die "Semaphore не вернул API token"

    printf '%s\n' "$token" >"$SEMAPHORE_API_TOKEN_FILE"
    chmod 0600 "$SEMAPHORE_API_TOKEN_FILE"
    AUTH_MODE="token"

    api_token GET /user/ >/dev/null         || die "Созданный Semaphore API token не проходит проверку"
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
    local repos id payload response

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
        response="$(api POST "/project/${project_id}/repositories" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
    else
        api PUT "/project/${project_id}/repositories/${id}" -d "$payload" >/dev/null
    fi

    [[ -n "$id" ]] || die "Semaphore не вернул id Git repository"
    printf '%s' "$id"
}

ensure_opentofu_environment() {
    local project_id=$1
    local endpoint token_id token_secret api_token
    local environments id existing secret_id env_json payload response

    endpoint="$(read_env_value PVE_API_URL "$PVE_API_ENV")"
    token_id="$(read_env_value PVE_API_TOKEN_ID "$PVE_API_ENV")"
    token_secret="$(read_env_value PVE_API_TOKEN_SECRET "$PVE_API_ENV")"
    [[ -n "$endpoint" && -n "$token_id" && -n "$token_secret" ]]         || die "PVE API credential неполон для OpenTofu"

    api_token="${token_id}=${token_secret}"
    env_json="$(jq -cn --arg endpoint "$endpoint" '{TF_VAR_pve_endpoint:$endpoint}')"

    environments="$(api GET "/project/${project_id}/environment?sort=name&order=asc")"
    id="$(jq -r --arg name "$OPENTOFU_ENV_NAME"         '.[] | select(.name == $name) | .id' <<<"$environments" | head -n1)"

    if [[ -z "$id" ]]; then
        payload="$(jq -cn             --arg name "$OPENTOFU_ENV_NAME"             --arg env "$env_json"             --arg secret "$api_token"             --argjson project_id "$project_id"             '{
                name:$name,
                project_id:$project_id,
                password:null,
                json:"{}",
                env:$env,
                secrets:[{
                    name:"TF_VAR_pve_api_token",
                    secret:$secret,
                    type:"env",
                    operation:"create"
                }]
            }')"
        response="$(api POST "/project/${project_id}/environment" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
        [[ -n "$id" ]] || die "Semaphore не вернул id Variable Group OpenTofu"
        printf '%s' "$id"
        return
    fi

    existing="$(api GET "/project/${project_id}/environment/${id}")"
    secret_id="$(jq -r         '.secrets[]? | select(.name == "TF_VAR_pve_api_token" and .type == "env") | .id'         <<<"$existing" | head -n1)"

    if [[ -z "$secret_id" ]]; then
        payload="$(jq -cn             --arg name "$OPENTOFU_ENV_NAME"             --arg env "$env_json"             --arg secret "$api_token"             --argjson id "$id"             --argjson project_id "$project_id"             '{
                id:$id,
                name:$name,
                project_id:$project_id,
                password:null,
                json:"{}",
                env:$env,
                secrets:[{
                    name:"TF_VAR_pve_api_token",
                    secret:$secret,
                    type:"env",
                    operation:"create"
                }]
            }')"
    elif [[ "$RECOVER" == "1" ]]; then
        payload="$(jq -cn             --arg name "$OPENTOFU_ENV_NAME"             --arg env "$env_json"             --arg secret "$api_token"             --argjson id "$id"             --argjson secret_id "$secret_id"             --argjson project_id "$project_id"             '{
                id:$id,
                name:$name,
                project_id:$project_id,
                password:null,
                json:"{}",
                env:$env,
                secrets:[{
                    id:$secret_id,
                    name:"TF_VAR_pve_api_token",
                    secret:$secret,
                    type:"env",
                    operation:"update"
                }]
            }')"
    else
        payload="$(jq -cn             --arg name "$OPENTOFU_ENV_NAME"             --arg env "$env_json"             --argjson id "$id"             --argjson project_id "$project_id"             '{
                id:$id,
                name:$name,
                project_id:$project_id,
                password:null,
                json:"{}",
                env:$env,
                secrets:[]
            }')"
    fi

    api PUT "/project/${project_id}/environment/${id}" -d "$payload" >/dev/null
    printf '%s' "$id"
}


ensure_opentofu_plan_template() {
    local project_id=$1 repository_id=$2 environment_id=$3
    local name="OpenTofu Plan"
    local templates id payload response

    templates="$(api GET "/project/${project_id}/templates?sort=name&order=asc")"
    id="$(jq -r --arg name "$name"         '.[] | select(.name == $name) | .id' <<<"$templates" | head -n1)"

    payload="$(jq -cn         --arg name "$name"         --arg playbook "scripts/infra-deployer/opentofu-plan.sh"         --arg branch "$PROJECT_BRANCH"         --argjson project_id "$project_id"         --argjson repository_id "$repository_id"         --argjson environment_id "$environment_id"         '{
            name:$name,
            project_id:$project_id,
            repository_id:$repository_id,
            environment_ids:[$environment_id],
            playbook:$playbook,
            app:"bash",
            type:"",
            git_branch:$branch,
            arguments:"[]",
            allow_override_args_in_task:false,
            allow_override_branch_in_task:false
        }')"

    if [[ -z "$id" ]]; then
        response="$(api POST "/project/${project_id}/templates" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
    else
        api PUT "/project/${project_id}/templates/${id}" -d "$payload" >/dev/null
    fi

    [[ -n "$id" ]] || die "Semaphore не вернул id шаблона OpenTofu Plan"
    printf '%s' "$id"
}

main() {
    local project_id github_key_id pve_key_id ansible_key_id repository_id opentofu_env_id opentofu_plan_template_id

    command -v jq >/dev/null 2>&1 || die "Не найден jq"
    command -v curl >/dev/null 2>&1 || die "Не найден curl"

    [[ -s "$PVE_API_ENV" ]] || die "Не найден постоянный PVE API credential"

    persist_github_key
    ensure_ansible_key
    ensure_api_token

    project_id="$(ensure_project)"
    github_key_id="$(ensure_ssh_key "$project_id" "GitHub project read-only" git "$GITHUB_KEY_COPY")"
    pve_key_id="$(ensure_pve_key "$project_id")"
    ansible_key_id="$(ensure_ssh_key "$project_id" "Ansible managed guests" root "$ANSIBLE_KEY")"
    repository_id="$(ensure_repository "$project_id" "$github_key_id")"
    opentofu_env_id="$(ensure_opentofu_environment "$project_id")"

    [[ -n "$pve_key_id" && -n "$ansible_key_id" && -n "$repository_id" && -n "$opentofu_env_id" ]]         || die "Не все объекты Semaphore созданы"
    ok "Проект Semaphore, Key Store, Git repository и OpenTofu Variable Group подготовлены"
}

main "$@"
