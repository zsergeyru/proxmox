#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -gt 0 ]] || { echo "Использование: run-runtime.sh КОМАНДА [АРГУМЕНТЫ...]" >&2; exit 2; }

REPO_ROOT="${BOOTSTRAP_RUNNER_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CONFIG_DIR="/etc/bootstrap-runner"
DATA_DIR="/var/lib/bootstrap-runner"
PVE_ENV="$CONFIG_DIR/secrets/pve-api.env"
IMAGE="${BOOTSTRAP_RUNNER_IMAGE:-bootstrap-runtime:v1}"

[[ -s "$PVE_ENV" ]] || { echo "ОШИБКА: не найден временный PVE API credential: $PVE_ENV" >&2; exit 1; }
[[ -s "$CONFIG_DIR/ca/ca-bundle.crt" ]] || { echo "ОШИБКА: не найден PVE CA" >&2; exit 1; }
[[ -s "$CONFIG_DIR/ansible/guest_ed25519.pub" ]] || { echo "ОШИБКА: не найден открытый SSH-ключ Ansible" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$PVE_ENV"
set +a

TF_VAR_ansible_ssh_public_key="$(cat "$CONFIG_DIR/ansible/guest_ed25519.pub")"
TF_VAR_guest_state_file="$DATA_DIR/opentofu/guests.json"
export TF_VAR_ansible_ssh_public_key TF_VAR_guest_state_file

exec docker run --rm     --network host     -e INFRA_MANAGER_CONFIG_DIR=/etc/bootstrap-runner     -e INFRA_MANAGER_DATA_DIR=/var/lib/bootstrap-runner     -e INFRA_PROJECT_BRANCH="${INFRA_PROJECT_BRANCH:-main}"     -e TF_VAR_pve_endpoint     -e TF_VAR_pve_api_token     -e TF_VAR_ansible_ssh_public_key     -e TF_VAR_guest_state_file     -v "$REPO_ROOT:/workspace"     -v "$CONFIG_DIR:/etc/bootstrap-runner:ro"     -v "$DATA_DIR:/var/lib/bootstrap-runner"     -w /workspace     "$IMAGE" "$@"
