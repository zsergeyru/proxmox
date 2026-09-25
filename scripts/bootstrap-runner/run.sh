#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-deploy}"
REPO_DIR="${BOOTSTRAP_REPO_DIR:-/opt/bootstrap-runner/repo}"
DATA_DIR="${BOOTSTRAP_DATA_DIR:-/var/lib/bootstrap-runner}"
CONFIG_DIR="${BOOTSTRAP_CONFIG_DIR:-/etc/bootstrap-runner}"
STATE_DIR="${DATA_DIR}/opentofu"
SSH_DIR="${DATA_DIR}/ssh"
WORK_DIR="${DATA_DIR}/work"
CA_FILE="${CONFIG_DIR}/ca/ca-bundle.crt"
PVE_ENV="${CONFIG_DIR}/secrets/pve-api.env"
PRIVATE_KEY="${SSH_DIR}/guest_ed25519"
PUBLIC_KEY="${PRIVATE_KEY}.pub"
RUNTIME_CONTEXT="${REPO_DIR}/infrastructure/guests/910-infra-manager/compose/runtime"
RUNTIME_IMAGE="infra-runtime:bootstrap"

INFRA_ADDRESS="${INFRA_MANAGER_ADDRESS:-192.168.9.10}"
KNOWN_HOSTS="${WORK_DIR}/guest-known-hosts"
REMOTE_REPO="/var/lib/infra-manager/bootstrap-repo"
REMOTE_PVE_SECRET="/root/.infra-manager-bootstrap/pve-api.env"
GITHUB_KEY="${BOOTSTRAP_GITHUB_KEY:-/root/.ssh/github_proxmox_repo_ed25519}"
GITHUB_PUB="${GITHUB_KEY}.pub"
GITHUB_KNOWN_HOSTS="${BOOTSTRAP_GITHUB_KNOWN_HOSTS:-/root/.ssh/github_known_hosts}"
GITHUB_CONFIG="${BOOTSTRAP_GITHUB_CONFIG:-/root/.ssh/github_config}"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
info() { printf '[ИНФО] %s\n' "$*"; }
ok() { printf '[ОК] %s\n' "$*"; }

require_base() {
    [[ $EUID -eq 0 ]] || die "bootstrap-runner должен выполняться от root внутри 990"
    [[ -f /etc/debian_version ]] || die "990 должен быть Debian"
    [[ -d "$REPO_DIR/.git" ]] || die "Не найден закрытый проект: $REPO_DIR"
    [[ -s "$PRIVATE_KEY" ]] || die "Не найден bootstrap SSH private key: $PRIVATE_KEY"
    [[ -s "$PUBLIC_KEY" ]] || die "Не найден bootstrap SSH public key: $PUBLIC_KEY"

    install -d -m 0700 "$STATE_DIR/state" "$SSH_DIR" "$WORK_DIR"
    chmod 0600 "$PRIVATE_KEY"
}

ensure_runtime() {
    [[ -f "$RUNTIME_CONTEXT/Dockerfile" ]] || die "Не найден Dockerfile infra-runtime"

    if ! command -v docker >/dev/null 2>&1; then
        info "Установка Docker внутри временного 990"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker.io
    fi

    systemctl start docker
    docker info >/dev/null

    info "Сборка единой среды infra-runtime"
    docker build -t "$RUNTIME_IMAGE" "$RUNTIME_CONTEXT"
}

deploy_910() {
    [[ -s "$PVE_ENV" ]] || die "Не найден временный PVE credential: $PVE_ENV"
    [[ -s "$CA_FILE" ]] || die "Не найден PVE CA bundle: $CA_FILE"
    chmod 0600 "$PVE_ENV"

    ensure_runtime

    set -a
    # shellcheck disable=SC1090
    . "$PVE_ENV"
    set +a

    : "${PVE_API_URL:?PVE_API_URL отсутствует в pve-api.env}"
    : "${PVE_API_TOKEN_ID:?PVE_API_TOKEN_ID отсутствует в pve-api.env}"
    : "${PVE_API_TOKEN_SECRET:?PVE_API_TOKEN_SECRET отсутствует в pve-api.env}"

    local ansible_public_key
    ansible_public_key="$(cat "$PUBLIC_KEY")"
    [[ -n "$ansible_public_key" ]] || die "Пустой bootstrap SSH public key"

    info "Развёртывание объекта и базовой ОС 910 общим guest-deploy"
    docker run --rm \
        --network host \
        -e "TF_VAR_pve_endpoint=$PVE_API_URL" \
        -e "TF_VAR_pve_api_token=$PVE_API_TOKEN_ID=$PVE_API_TOKEN_SECRET" \
        -e "TF_VAR_ansible_ssh_public_key=$ansible_public_key" \
        -v "$REPO_DIR:/workspace" \
        -v "$CONFIG_DIR/ca:/etc/infra-manager/ca:ro" \
        -v "$SSH_DIR:/etc/infra-manager/ansible:ro" \
        -v "$STATE_DIR:/var/lib/infra-manager/opentofu" \
        -v "$WORK_DIR:/var/lib/semaphore" \
        -w /workspace \
        --entrypoint python3 \
        "$RUNTIME_IMAGE" \
        /workspace/scripts/bootstrap-runner/deploy-910.py

    [[ -s "$STATE_DIR/state/proxmox.tfstate" ]] \
        || die "После deploy-guest отсутствует bootstrap-state 910"

    ok "Объект и базовая ОС 910 готовы; требуется постоянный PVE-доступ 910"
}

ssh_910() {
    ssh \
        -i "$PRIVATE_KEY" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$KNOWN_HOSTS" \
        "root@$INFRA_ADDRESS" "$@"
}

copy_910() {
    scp \
        -i "$PRIVATE_KEY" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$KNOWN_HOSTS" \
        "$1" "root@$INFRA_ADDRESS:$2"
}

configure_910() {
    command -v ssh >/dev/null 2>&1 || die "В 990 отсутствует ssh"
    command -v scp >/dev/null 2>&1 || die "В 990 отсутствует scp"
    [[ -s "$KNOWN_HOSTS" ]] || die "Не найден known_hosts 910 после deploy"
    [[ -s "$GITHUB_KEY" ]] || die "Не найден GitHub Deploy Key внутри 990"
    [[ -s "$GITHUB_PUB" ]] || die "Не найден public GitHub Deploy Key внутри 990"
    [[ -s "$GITHUB_KNOWN_HOSTS" ]] || die "Не найден GitHub known_hosts внутри 990"
    [[ -s "$GITHUB_CONFIG" ]] || die "Не найден GitHub ssh config внутри 990"

    ssh_910 test -s "$REMOTE_PVE_SECRET" \
        || die "В 910 ещё не передан постоянный PVE API credential"

    info "Передача зафиксированной ревизии проекта в 910"
    ssh_910 "rm -rf '$REMOTE_REPO'; install -d -m 0755 '$REMOTE_REPO'"
    tar -C "$REPO_DIR" -cf - . \
        | ssh_910 "tar -C '$REMOTE_REPO' -xf -"

    info "Передача read-only GitHub Deploy Key в 910"
    ssh_910 "install -d -m 0700 /root/.ssh"
    copy_910 "$GITHUB_KEY" /root/.ssh/github_proxmox_repo_ed25519
    copy_910 "$GITHUB_PUB" /root/.ssh/github_proxmox_repo_ed25519.pub
    copy_910 "$GITHUB_KNOWN_HOSTS" /root/.ssh/github_known_hosts
    copy_910 "$GITHUB_CONFIG" /root/.ssh/github_config
    ssh_910 "chmod 0600 /root/.ssh/github_proxmox_repo_ed25519 /root/.ssh/github_config; chmod 0644 /root/.ssh/github_proxmox_repo_ed25519.pub /root/.ssh/github_known_hosts"

    info "Полная внутренняя настройка 910"
    ssh_910 \
        "INFRA_MANAGER_BOOTSTRAP=1 INFRA_MANAGER_COLOR=0 INFRA_PROJECT_BRANCH='${INFRA_PROJECT_BRANCH:-main}' PVE_API_SECRET_FILE='$REMOTE_PVE_SECRET' bash '$REMOTE_REPO/scripts/infra-manager/setup.sh'"

    ssh_910 /usr/local/sbin/infra-manager-status --full
    ok "910 полностью настроен и прошёл проверку"
}

main() {
    require_base
    case "$MODE" in
        deploy) deploy_910 ;;
        configure) configure_910 ;;
        *)
            die "Использование: run.sh [deploy|configure]"
            ;;
    esac
}

main
