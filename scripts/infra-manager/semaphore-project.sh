#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s inherit_errexit

SEMAPHORE_URL="http://127.0.0.1:3000"
PROJECT_NAME="Proxmox Infrastructure"
OPENTOFU_ENV_NAME="OpenTofu PVE"
PROJECT_ID_FILE="/var/lib/infra-manager/semaphore-project-id"

SECRET_DIR="/etc/infra-manager/secrets"
ADMIN_PASSWORD_FILE="${SECRET_DIR}/initial-admin-password"
SEMAPHORE_API_TOKEN_FILE="${SECRET_DIR}/semaphore-api-token"
PVE_API_ENV="${SECRET_DIR}/pve-api.env"
GITHUB_KEY="/root/.ssh/github_proxmox_repo_ed25519"
GITHUB_KEY_COPY="${SECRET_DIR}/github_project_ed25519"

PROJECT_REPO="git@github.com:zsergeyru/proxmox.git"
PROJECT_BRANCH="${INFRA_PROJECT_BRANCH:-main}"

COOKIE=""
AUTH_MODE="cookie"

C_RESET=""
C_BOLD=""
C_GREEN=""
C_RED=""

if [[ "${INFRA_MANAGER_COLOR:-0}" == "1" && "${NO_COLOR:-}" == "" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
fi

die() { printf '%s%sОШИБКА:%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
ok()  { printf '%s%s[ОК]%s %s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$*"; }

cleanup() {
    [[ -z "$COOKIE" ]] || rm -f "$COOKIE"
}
trap cleanup EXIT

read_env_value() {
    local key=$1 file=$2
    sed -n "s/^${key}=//p" "$file" | head -n1
}

unique_id_by_name() {
    local json=$1 name=$2 kind=$3 count

    count="$(jq -r --arg name "$name" '[.[] | select(.name == $name)] | length' <<<"$json")"
    [[ "$count" =~ ^[0-9]+$ ]] || die "Не удалось проверить дубликаты Semaphore: $kind '$name'"
    ((count <= 1)) || die "В Semaphore найдено несколько объектов $kind с именем '$name'"

    if ((count == 1)); then
        jq -r --arg name "$name" '.[] | select(.name == $name) | .id' <<<"$json"
    fi
}

api_call() {
    local method=$1 path=$2
    shift 2
    local body_file http_code rc

    body_file="$(mktemp /tmp/semaphore-api-body.XXXXXX)"
    rc=0
    http_code="$(curl -sS -o "$body_file" -w '%{http_code}' \
        -X "$method" \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        "$@" "${SEMAPHORE_URL}/api${path}")" || rc=$?

    if ((rc != 0)); then
        [[ ! -s "$body_file" ]] || cat "$body_file" >&2
        rm -f "$body_file"
        return "$rc"
    fi

    if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
        printf 'Semaphore API %s %s вернул HTTP %s: ' "$method" "$path" "$http_code" >&2
        if [[ -s "$body_file" ]]; then
            cat "$body_file" >&2
            printf '\n' >&2
        else
            printf '(пустой ответ)\n' >&2
        fi
        rm -f "$body_file"
        return 22
    fi

    cat "$body_file"
    rm -f "$body_file"
}

api_cookie() {
    local method=$1 path=$2
    shift 2
    api_call "$method" "$path" -b "$COOKIE" "$@"
}

api_token() {
    local method=$1 path=$2 token
    shift 2
    token="$(cat "$SEMAPHORE_API_TOKEN_FILE")"
    api_call "$method" "$path" -H "Authorization: Bearer $token" "$@"
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
    payload='{"name":"infra-manager setup"}'
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
    id="$(unique_id_by_name "$projects" "$PROJECT_NAME" "project")"

    if [[ -z "$id" ]]; then
        payload="$(jq -n --arg name "$PROJECT_NAME"             '{name:$name,alert:false,max_parallel_tasks:1,demo:false}')"
        response="$(api POST /projects -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
        [[ -n "$id" ]] || die "Semaphore создал проект, но не вернул id"
    fi

    printf '%s\n' "$id" >"$PROJECT_ID_FILE"
    chmod 0640 "$PROJECT_ID_FILE"
    printf '%s' "$id"
}

key_id_by_name() {
    local project_id=$1 name=$2 keys
    keys="$(api GET "/project/${project_id}/keys?sort=name&order=asc")"
    unique_id_by_name "$keys" "$name" "SSH key"
}

ensure_ssh_key() {
    local project_id=$1 name=$2 login_name=$3 private_key_file=$4
    local id private_key payload response

    id="$(key_id_by_name "$project_id" "$name")"
    private_key="$(cat "$private_key_file")"

    if [[ -z "$id" ]]; then
        payload="$(jq -n \
            --arg name "$name" \
            --arg login "$login_name" \
            --arg private_key "$private_key" \
            --argjson project_id "$project_id" \
            '{name:$name,type:"ssh",project_id:$project_id,ssh:{login:$login,passphrase:"",private_key:$private_key}}')"

        response="$(api POST "/project/${project_id}/keys" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
        [[ -n "$id" ]] || die "Не удалось создать SSH key '$name' в Semaphore"
    else
        payload="$(jq -n \
            --argjson id "$id" \
            --arg name "$name" \
            --arg login "$login_name" \
            --arg private_key "$private_key" \
            --argjson project_id "$project_id" \
            '{id:$id,name:$name,type:"ssh",project_id:$project_id,ssh:{login:$login,passphrase:"",private_key:$private_key}}')"

        api PUT "/project/${project_id}/keys/${id}" -d "$payload" >/dev/null
    fi

    printf '%s' "$id"
}

persist_github_key() {
    [[ -s "$GITHUB_KEY" ]] || die "GitHub Deploy Key не найден: $GITHUB_KEY"

    if [[ ! -f "$GITHUB_KEY_COPY" ]] || ! cmp -s "$GITHUB_KEY" "$GITHUB_KEY_COPY"; then
        install -o root -g root -m 0600 "$GITHUB_KEY" "$GITHUB_KEY_COPY"
        ok "GitHub Deploy Key синхронизирован с постоянным ключом PVE"
    fi
}

ensure_repository() {
    local project_id=$1 ssh_key_id=$2
    local repos id payload response

    repos="$(api GET "/project/${project_id}/repositories?sort=name&order=asc")"
    id="$(unique_id_by_name "$repos" "proxmox" "Git repository")"

    if [[ -z "$id" ]]; then
        payload="$(jq -n \
            --arg name proxmox \
            --arg git_url "$PROJECT_REPO" \
            --arg git_branch "$PROJECT_BRANCH" \
            --argjson project_id "$project_id" \
            --argjson ssh_key_id "$ssh_key_id" \
            '{name:$name,project_id:$project_id,git_url:$git_url,git_branch:$git_branch,ssh_key_id:$ssh_key_id}')"
        response="$(api POST "/project/${project_id}/repositories" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
    else
        payload="$(jq -n \
            --argjson id "$id" \
            --arg name proxmox \
            --arg git_url "$PROJECT_REPO" \
            --arg git_branch "$PROJECT_BRANCH" \
            --argjson project_id "$project_id" \
            --argjson ssh_key_id "$ssh_key_id" \
            '{id:$id,name:$name,project_id:$project_id,git_url:$git_url,git_branch:$git_branch,ssh_key_id:$ssh_key_id}')"
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
    id="$(unique_id_by_name "$environments" "$OPENTOFU_ENV_NAME" "Variable Group")"

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
    else
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
    fi

    api PUT "/project/${project_id}/environment/${id}" -d "$payload" >/dev/null
    printf '%s' "$id"
}


ensure_opentofu_plan_template() {
    local project_id=$1 repository_id=$2 environment_id=$3
    local name="OpenTofu Plan"
    local templates id payload response

    templates="$(api GET "/project/${project_id}/templates?sort=name&order=asc")"
    id="$(unique_id_by_name "$templates" "$name" "template")"

    if [[ -z "$id" ]]; then
        payload="$(jq -cn         --arg name "$name"         --arg playbook "scripts/infra-manager/opentofu-plan.sh"         --arg branch "$PROJECT_BRANCH"         --argjson project_id "$project_id"         --argjson repository_id "$repository_id"         --argjson environment_id "$environment_id"         '{
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
        response="$(api POST "/project/${project_id}/templates" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
    else
        payload="$(jq -cn         --argjson id "$id"         --arg name "$name"         --arg playbook "scripts/infra-manager/opentofu-plan.sh"         --arg branch "$PROJECT_BRANCH"         --argjson project_id "$project_id"         --argjson repository_id "$repository_id"         --argjson environment_id "$environment_id"         '{
                id:$id,
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
        api PUT "/project/${project_id}/templates/${id}" -d "$payload" >/dev/null
    fi

    [[ -n "$id" ]] || die "Semaphore не вернул id шаблона OpenTofu Plan"
    printf '%s' "$id"
}


ensure_packer_9000_template() {
    local project_id=$1 repository_id=$2 environment_id=$3
    local name="Build Template 9000"
    local templates id payload response

    templates="$(api GET "/project/${project_id}/templates?sort=name&order=asc")"
    id="$(unique_id_by_name "$templates" "$name" "template")"

    if [[ -z "$id" ]]; then
        payload="$(jq -cn \
            --arg name "$name" \
            --arg playbook "scripts/infra-manager/build-template.sh" \
            --arg branch "$PROJECT_BRANCH" \
            --argjson project_id "$project_id" \
            --argjson repository_id "$repository_id" \
            --argjson environment_id "$environment_id" \
            '{
                name:$name,
                project_id:$project_id,
                repository_id:$repository_id,
                environment_ids:[$environment_id],
                playbook:$playbook,
                app:"bash",
                type:"",
                git_branch:$branch,
                arguments:"[\"9000\"]",
                allow_override_args_in_task:false,
                allow_override_branch_in_task:false
            }')"
        response="$(api POST "/project/${project_id}/templates" -d "$payload")"
        id="$(jq -r '.id // empty' <<<"$response")"
    else
        payload="$(jq -cn \
            --argjson id "$id" \
            --arg name "$name" \
            --arg playbook "scripts/infra-manager/build-template.sh" \
            --arg branch "$PROJECT_BRANCH" \
            --argjson project_id "$project_id" \
            --argjson repository_id "$repository_id" \
            --argjson environment_id "$environment_id" \
            '{
                id:$id,
                name:$name,
                project_id:$project_id,
                repository_id:$repository_id,
                environment_ids:[$environment_id],
                playbook:$playbook,
                app:"bash",
                type:"",
                git_branch:$branch,
                arguments:"[\"9000\"]",
                allow_override_args_in_task:false,
                allow_override_branch_in_task:false
            }')"
        api PUT "/project/${project_id}/templates/${id}" -d "$payload" >/dev/null
    fi

    [[ -n "$id" ]] || die "Semaphore не вернул id шаблона Build Template 9000"
    printf '%s' "$id"
}

main() {
    local project_id github_key_id repository_id opentofu_env_id opentofu_plan_template_id packer_9000_template_id

    command -v jq >/dev/null 2>&1 || die "Не найден jq"
    command -v curl >/dev/null 2>&1 || die "Не найден curl"

    [[ -s "$PVE_API_ENV" ]] || die "Не найден постоянный PVE API credential"

    persist_github_key
    ensure_api_token

    project_id="$(ensure_project)"

    github_key_id="$(ensure_ssh_key "$project_id" "GitHub project read-only" git "$GITHUB_KEY_COPY")"
    repository_id="$(ensure_repository "$project_id" "$github_key_id")"
    opentofu_env_id="$(ensure_opentofu_environment "$project_id")"
    opentofu_plan_template_id="$(ensure_opentofu_plan_template "$project_id" "$repository_id" "$opentofu_env_id")"
    packer_9000_template_id="$(ensure_packer_9000_template "$project_id" "$repository_id" "$opentofu_env_id")"

    [[ -n "$repository_id" && -n "$opentofu_env_id" && -n "$opentofu_plan_template_id" && -n "$packer_9000_template_id" ]] \
        || die "Не все объекты Semaphore созданы"

    ok "Проект Semaphore, Git repository, PVE Variable Group и инфраструктурные задания подготовлены"
}

main "$@"
