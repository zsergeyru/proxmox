#!/usr/bin/env bash
set -Eeuo pipefail

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

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
info() { printf '[ИНФО] %s\n' "$*"; }
ok() { printf '[ОК] %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "bootstrap-runner должен выполняться от root внутри 990"
[[ -f /etc/debian_version ]] || die "990 должен быть Debian"
[[ -d "$REPO_DIR/.git" ]] || die "Не найден закрытый проект: $REPO_DIR"
[[ -s "$PVE_ENV" ]] || die "Не найден временный PVE credential: $PVE_ENV"
[[ -s "$CA_FILE" ]] || die "Не найден PVE CA bundle: $CA_FILE"
[[ -s "$PRIVATE_KEY" ]] || die "Не найден bootstrap SSH private key: $PRIVATE_KEY"
[[ -s "$PUBLIC_KEY" ]] || die "Не найден bootstrap SSH public key: $PUBLIC_KEY"
[[ -f "$RUNTIME_CONTEXT/Dockerfile" ]] || die "Не найден Dockerfile infra-runtime"

install -d -m 0700 "$STATE_DIR/state" "$SSH_DIR" "$WORK_DIR"
chmod 0600 "$PRIVATE_KEY" "$PVE_ENV"

if ! command -v docker >/dev/null 2>&1; then
    info "Установка Docker внутри временного 990"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker.io
fi

systemctl start docker
docker info >/dev/null

info "Сборка единой среды infra-runtime"
docker build -t "$RUNTIME_IMAGE" "$RUNTIME_CONTEXT"

set -a
# shellcheck disable=SC1090
. "$PVE_ENV"
set +a

: "${PVE_API_URL:?PVE_API_URL отсутствует в pve-api.env}"
: "${PVE_API_TOKEN_ID:?PVE_API_TOKEN_ID отсутствует в pve-api.env}"
: "${PVE_API_TOKEN_SECRET:?PVE_API_TOKEN_SECRET отсутствует в pve-api.env}"

ANSIBLE_PUBLIC_KEY="$(cat "$PUBLIC_KEY")"
[[ -n "$ANSIBLE_PUBLIC_KEY" ]] || die "Пустой bootstrap SSH public key"

info "Развёртывание 910 общим guest-deploy"
docker run --rm \
    --network host \
    -e "TF_VAR_pve_endpoint=$PVE_API_URL" \
    -e "TF_VAR_pve_api_token=$PVE_API_TOKEN_ID=$PVE_API_TOKEN_SECRET" \
    -e "TF_VAR_ansible_ssh_public_key=$ANSIBLE_PUBLIC_KEY" \
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

ok "910 развернут; bootstrap-state готов к возврату на PVE"
