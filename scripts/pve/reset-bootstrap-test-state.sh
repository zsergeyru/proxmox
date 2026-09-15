#!/usr/bin/env bash
set -Eeuo pipefail

# Reset only project-owned Proxmox bootstrap state so the bootstrap can be
# exercised again from a nearly clean state without touching unrelated guests,
# networking, base storages, package repositories, or retained safety backups.
#
# Default mode is dry-run. Use --apply for destructive changes.
#
# By default the current canonical GitHub Deploy Key is copied back into the
# Stage 0 temporary area before /etc/proxmox-deployer is removed. This lets the
# next Stage 0 verify the already-authorized key and recreate the canonical
# layout without requiring a new GitHub Deploy Key registration.

APPLY=0
DROP_DEPLOY_KEY=0
RESTORE_STORAGE_BASELINE=0

TEMPLATE_VMID=9000
TEMPLATE_NAME="tpl-debian13"
BUILDER_NAME="builder-debian13"
MANAGED_POOL="managed"
DEPLOY_USER="pvedeploy"

HOST_PVE_USER="deployer@pve"
HOST_PVE_TOKEN_NAME="host-deploy"
HOST_PVE_TOKEN="${HOST_PVE_USER}!${HOST_PVE_TOKEN_NAME}"
AI_PVE_USER="ai-agent@pve"
AI_PVE_TOKEN_NAME="infra"
AI_PVE_TOKEN="${AI_PVE_USER}!${AI_PVE_TOKEN_NAME}"

ROLE_CLONE="AICloneSource"
ROLE_GUEST="AIManagedGuest"
ROLE_NETWORK="AINetworkUse"
ROLE_STORAGE="AIStorage"
ROLE_POOL="AIManagedPool"
PROJECT_ROLES=(
    "$ROLE_CLONE"
    "$ROLE_GUEST"
    "$ROLE_NETWORK"
    "$ROLE_STORAGE"
    "$ROLE_POOL"
)

CONFIG_DIR="/etc/proxmox-deployer"
CANONICAL_KEY="${CONFIG_DIR}/ssh/github_proxmox_repo_ed25519"
CANONICAL_KNOWN_HOSTS="${CONFIG_DIR}/ssh/known_hosts"
RUNTIME_DIR="/var/lib/proxmox-deployer"
STAGE0_DIR="/var/lib/proxmox-bootstrap"
STAGE0_KEY="${STAGE0_DIR}/github_proxmox_repo_ed25519"
STAGE0_KNOWN_HOSTS="${STAGE0_DIR}/known_hosts"
LOG_DIR="/var/log/proxmox-deployer"
BOOTSTRAP_LOG_DIR="/var/log/proxmox-bootstrap"
BACKUP_ROOT="/var/backups/proxmox-bootstrap"
RESET_BACKUP_ROOT="/var/backups/proxmox-test-reset"
LOCK_FILE="/run/lock/proxmox-bootstrap-init.lock"

LXC_TEMPLATE_STORAGE="local"
CLOUD_IMAGE_CACHE="/var/lib/vz/template/cache/debian13"
BUILDER_SNIPPET="/var/lib/vz/snippets/debian13-template-builder-${TEMPLATE_VMID}.yaml"

TEMP_SELF="${PVE_RESET_TEMP_SELF:-}"

log() { printf '\n==> %s\n' "$*"; }
ok() { printf '[ОК] %s\n' "$*"; }
info() { printf '[ИНФО] %s\n' "$*"; }
warn() { printf '[ПРЕДУПРЕЖДЕНИЕ] %s\n' "$*" >&2; }
die() { printf '\nОШИБКА: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF_USAGE'
Использование:
  reset-bootstrap-test-state.sh [--apply] [--drop-deploy-key] [--restore-storage-baseline]

Без --apply скрипт работает только в режиме dry-run.

Параметры:
  --apply
      Реально выполнить сброс.

  --drop-deploy-key
      Не переносить текущий canonical GitHub Deploy Key обратно во временную
      область Stage 0. Следующий Stage 0 создаст новый ключ и потребует заново
      зарегистрировать его как Deploy Key на GitHub.

  --restore-storage-baseline
      Дополнительно восстановить content types storage local/local-lvm по самому
      раннему сохранённому bootstrap snapshot. Используйте только если хотите
      протестировать именно добавление content types. Файл storage.cfg целиком
      НЕ восстанавливается.

Что удаляется/сбрасывается при --apply:
  - VM/template 9000, только если это tpl-debian13 или builder-debian13;
  - Debian 13 LXC template cache на storage local;
  - cache generic cloud image и временный builder snippet;
  - API tokens deployer@pve!host-deploy и ai-agent@pve!infra;
  - PVE users deployer@pve и ai-agent@pve;
  - проектные роли AICloneSource/AIManagedGuest/AINetworkUse/AIStorage/AIManagedPool,
    но роль не удаляется, если ею пользуется посторонний principal;
  - pool managed, только если он пуст;
  - /etc/proxmox-deployer, /var/lib/proxmox-deployer и проектные логи;
  - Linux user/group pvedeploy;
  - /usr/local/sbin/pve-bootstrap-status и /usr/local/sbin/deploy-guest.

Что принципиально НЕ удаляется:
  - любые другие VM/LXC, включая VM 100;
  - гости из managed (если они есть, pool просто не удаляется);
  - vmbr0, local, local-lvm и сами storage volumes посторонних гостей;
  - PVE/Debian repository configuration;
  - установленные системные пакеты;
  - /var/backups/proxmox-bootstrap и другие существующие backup snapshots.
EOF_USAGE
}

while (($#)); do
    case "$1" in
        --apply) APPLY=1 ;;
        --drop-deploy-key) DROP_DEPLOY_KEY=1 ;;
        --restore-storage-baseline) RESTORE_STORAGE_BASELINE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Неизвестный параметр: $1" ;;
    esac
    shift
done

cleanup() {
    if [[ -n "${TEMP_SELF:-}" ]]; then
        rm -f -- "$TEMP_SELF" 2>/dev/null || true
    fi
}
trap cleanup EXIT

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Не найдена обязательная команда: $1"
}

print_cmd() {
    printf '[DRY-RUN]'
    printf ' %q' "$@"
    printf '\n'
}

do_cmd() {
    if (( APPLY )); then
        "$@"
    else
        print_cmd "$@"
    fi
}

# The normal execution path lives inside /var/lib/proxmox-deployer/repo, which
# is intentionally deleted by this reset. Re-exec a private copy from /run so
# removing the checkout can never truncate the running script.
relocate_self_if_needed() {
    local self_path tmp
    [[ "${PVE_RESET_REEXEC:-0}" == "1" ]] && return

    self_path="$(readlink -f "$0" 2>/dev/null || true)"
    [[ "$self_path" == "${RUNTIME_DIR}/"* ]] || return

    tmp="/run/proxmox-bootstrap-test-reset.$$"
    cp -- "$self_path" "$tmp"
    chmod 0700 "$tmp"
    exec env PVE_RESET_REEXEC=1 PVE_RESET_TEMP_SELF="$tmp" bash "$tmp" \
        $([[ $APPLY -eq 1 ]] && printf '%s ' --apply) \
        $([[ $DROP_DEPLOY_KEY -eq 1 ]] && printf '%s ' --drop-deploy-key) \
        $([[ $RESTORE_STORAGE_BASELINE -eq 1 ]] && printf '%s ' --restore-storage-baseline)
}

relocate_self_if_needed

[[ $EUID -eq 0 ]] || die "Запустите скрипт от root на хосте Proxmox"
for cmd in \
    pveversion qm pct pvesh pveum pvesm pveam jq awk grep sed sort find install \
    cp rm mkdir chmod chown userdel groupdel id getent flock readlink sha256sum; do
    require_cmd "$cmd"
done

pve_line="$(pveversion | head -n1)"
[[ "$pve_line" =~ ^pve-manager/9\. ]] \
    || die "Скрипт рассчитан на Proxmox VE 9.x, обнаружено: ${pve_line:-неизвестно}"

install -d -m 0755 /run/lock
exec 9>"$LOCK_FILE"
flock -n 9 || die "Приватный bootstrap сейчас выполняется. Дождитесь его завершения и повторите сброс."
ok "Получена блокировка bootstrap; параллельный init исключён"

if (( APPLY )); then
    warn "РЕЖИМ APPLY: проектное bootstrap-состояние будет удалено. Посторонние VM/LXC не затрагиваются."
else
    info "DRY-RUN: изменений на PVE не будет. Для выполнения используйте --apply."
fi

create_reset_backup() {
    (( APPLY )) || return

    local stamp dst
    stamp="$(date +%Y%m%d-%H%M%S)"
    dst="${RESET_BACKUP_ROOT}/${stamp}"
    install -d -o root -g root -m 0700 "$dst"

    cp -a /etc/pve/user.cfg "$dst/user.cfg" 2>/dev/null || true
    cp -a /etc/pve/storage.cfg "$dst/storage.cfg" 2>/dev/null || true
    pveum user list --full 1 --output-format json >"$dst/pve-users.json" 2>/dev/null || true
    pveum role list --output-format json >"$dst/pve-roles.json" 2>/dev/null || true
    pveum acl list --output-format json >"$dst/pve-acl.json" 2>/dev/null || true
    pveum pool list --output-format json >"$dst/pve-pools.json" 2>/dev/null || true
    qm config "$TEMPLATE_VMID" >"$dst/vm-${TEMPLATE_VMID}.conf" 2>/dev/null || true

    if [[ -d "$CONFIG_DIR" ]]; then
        cp -a "$CONFIG_DIR" "$dst/proxmox-deployer-etc" 2>/dev/null || true
    fi

    chmod -R go-rwx "$dst"
    ok "Перед сбросом сохранён закрытый snapshot конфигурации: $dst"
}

seed_stage0_deploy_key() {
    if (( DROP_DEPLOY_KEY )); then
        info "Canonical Deploy Key не будет сохранён; следующий Stage 0 создаст новый ключ"
        return
    fi

    if [[ ! -f "$CANONICAL_KEY" ]]; then
        warn "Canonical Deploy Key ${CANONICAL_KEY} отсутствует; сохранить его для Stage 0 невозможно"
        return
    fi

    if (( APPLY )); then
        install -d -o root -g root -m 0700 "$STAGE0_DIR"
        install -o root -g root -m 0600 "$CANONICAL_KEY" "$STAGE0_KEY"

        if [[ -f "${CANONICAL_KEY}.pub" ]]; then
            install -o root -g root -m 0644 "${CANONICAL_KEY}.pub" "${STAGE0_KEY}.pub"
        else
            ssh-keygen -y -f "$CANONICAL_KEY" >"${STAGE0_KEY}.pub"
            chmod 0644 "${STAGE0_KEY}.pub"
        fi

        if [[ -f "$CANONICAL_KNOWN_HOSTS" ]]; then
            install -o root -g root -m 0644 "$CANONICAL_KNOWN_HOSTS" "$STAGE0_KNOWN_HOSTS"
        fi
        ok "Текущий GitHub Deploy Key перенесён обратно во временную область Stage 0"
    else
        print_cmd install -d -m 0700 "$STAGE0_DIR"
        print_cmd install -m 0600 "$CANONICAL_KEY" "$STAGE0_KEY"
        info "При apply canonical Deploy Key будет сохранён для повторного Stage 0"
    fi
}

project_template_owned() {
    local cfg name template_flag
    cfg="$(qm config "$TEMPLATE_VMID" 2>/dev/null)" || return 1
    name="$(awk -F': ' '$1=="name" {print $2; exit}' <<<"$cfg")"
    template_flag="$(awk -F': ' '$1=="template" {print $2; exit}' <<<"$cfg")"

    if [[ "$name" == "$TEMPLATE_NAME" && "$template_flag" == "1" ]]; then
        return 0
    fi
    if [[ "$name" == "$BUILDER_NAME" && "$template_flag" != "1" ]]; then
        return 0
    fi
    return 1
}

destroy_project_template() {
    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} занят LXC-контейнером. Автоматическое удаление запрещено."
    fi

    if ! qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        ok "VMID ${TEMPLATE_VMID} уже отсутствует"
        return
    fi

    project_template_owned \
        || die "VMID ${TEMPLATE_VMID} существует, но не похож на ${TEMPLATE_NAME}/${BUILDER_NAME}. Удаление запрещено."

    local protection
    protection="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2; exit}')"

    if (( ! APPLY )); then
        [[ "$protection" == "1" ]] && print_cmd qm set "$TEMPLATE_VMID" --protection 0
        print_cmd qm destroy "$TEMPLATE_VMID" --purge 1 --destroy-unreferenced-disks 1
        return
    fi

    if [[ "$protection" == "1" ]]; then
        qm set "$TEMPLATE_VMID" --protection 0
    fi

    if ! qm destroy "$TEMPLATE_VMID" --purge 1 --destroy-unreferenced-disks 1; then
        if qm config "$TEMPLATE_VMID" >/dev/null 2>&1 && [[ "$protection" == "1" ]]; then
            qm set "$TEMPLATE_VMID" --protection 1 >/dev/null 2>&1 || true
        fi
        die "Не удалось безопасно удалить VM/template ${TEMPLATE_VMID}"
    fi

    ok "Удалён проектный VM/template ${TEMPLATE_VMID} вместе с принадлежащими ему дисками"
}

remove_template_caches() {
    local vol
    local -a volumes=()

    while IFS= read -r vol; do
        [[ -n "$vol" ]] && volumes+=("$vol")
    done < <(
        pveam list "$LXC_TEMPLATE_STORAGE" 2>/dev/null \
            | awk 'NR>1 {print $1}' \
            | grep -E "^${LXC_TEMPLATE_STORAGE}:vztmpl/debian-13-standard_.*_amd64\\.tar\\.(zst|xz|gz)$" \
            || true
    )

    if (( ${#volumes[@]} == 0 )); then
        ok "Debian 13 LXC template cache уже отсутствует"
    else
        for vol in "${volumes[@]}"; do
            do_cmd pveam remove "$vol"
        done
        (( APPLY )) && ok "Удалён Debian 13 LXC template cache: ${#volumes[@]} файл(ов)"
    fi

    if [[ -e "$CLOUD_IMAGE_CACHE" ]]; then
        do_cmd rm -rf -- "$CLOUD_IMAGE_CACHE"
        (( APPLY )) && ok "Удалён cache generic Debian cloud image: ${CLOUD_IMAGE_CACHE}"
    else
        ok "Cache generic Debian cloud image уже отсутствует"
    fi

    if [[ -e "$BUILDER_SNIPPET" ]]; then
        do_cmd rm -f -- "$BUILDER_SNIPPET"
        (( APPLY )) && ok "Удалён временный builder snippet"
    fi
}

pve_user_exists() {
    local userid=$1
    pveum user list --output-format json 2>/dev/null \
        | jq -e --arg id "$userid" '.[] | select(.userid == $id)' >/dev/null
}

token_exists() {
    local userid=$1 tokenid=$2
    pve_user_exists "$userid" || return 1
    pveum user token list "$userid" --output-format json 2>/dev/null \
        | jq -e --arg id "$tokenid" '.[] | select(.tokenid == $id)' >/dev/null
}

remove_pve_identity() {
    local userid=$1 tokenid=$2 full_token=$3

    if token_exists "$userid" "$tokenid"; then
        do_cmd pveum user token delete "$userid" "$tokenid"
        (( APPLY )) && ok "Удалён API-токен ${full_token}"
    else
        ok "API-токен ${full_token} уже отсутствует"
    fi

    if pve_user_exists "$userid"; then
        do_cmd pveum user delete "$userid"
        (( APPLY )) && ok "Удалён PVE user ${userid}"
    else
        ok "PVE user ${userid} уже отсутствует"
    fi
}

role_exists() {
    local role=$1
    pveum role list --output-format json 2>/dev/null \
        | jq -e --arg id "$role" '.[] | select(.roleid == $id)' >/dev/null
}

role_has_foreign_acl() {
    local role=$1
    pveum acl list --output-format json 2>/dev/null \
        | jq -e \
            --arg role "$role" \
            --arg host_user "$HOST_PVE_USER" \
            --arg host_token "$HOST_PVE_TOKEN" \
            --arg ai_user "$AI_PVE_USER" \
            --arg ai_token "$AI_PVE_TOKEN" '
                .[]
                | select(.roleid == $role)
                | select(
                    .ugid != $host_user
                    and .ugid != $host_token
                    and .ugid != $ai_user
                    and .ugid != $ai_token
                )
            ' >/dev/null
}

remove_project_roles() {
    local role
    for role in "${PROJECT_ROLES[@]}"; do
        if ! role_exists "$role"; then
            ok "Роль ${role} уже отсутствует"
            continue
        fi

        if role_has_foreign_acl "$role"; then
            warn "Роль ${role} используется посторонним principal; роль оставлена без изменений"
            continue
        fi

        do_cmd pveum role delete "$role"
        (( APPLY )) && ok "Удалена проектная роль ${role}"
    done
}

managed_pool_exists() {
    pveum pool list --output-format json 2>/dev/null \
        | jq -e --arg id "$MANAGED_POOL" '.[] | select(.poolid == $id)' >/dev/null
}

remove_managed_pool_if_empty() {
    if ! managed_pool_exists; then
        ok "Pool ${MANAGED_POOL} уже отсутствует"
        return
    fi

    local pool_json members_count
    pool_json="$(pvesh get "/pools/${MANAGED_POOL}" --output-format json 2>/dev/null || true)"
    members_count="$(jq -r '(.members // []) | length' <<<"${pool_json:-{}}" 2>/dev/null || printf 'unknown')"

    if [[ "$members_count" != "0" ]]; then
        warn "Pool ${MANAGED_POOL} не пуст (${members_count} member(s)); гости/storage НЕ удаляются, pool оставлен"
        if [[ "$members_count" =~ ^[0-9]+$ ]] && (( members_count > 0 )); then
            jq -r '(.members // [])[] | "       - " + (.type // "unknown") + " " + ((.vmid // .storage // .id // "?") | tostring)' \
                <<<"$pool_json" >&2 || true
        fi
        return
    fi

    do_cmd pveum pool delete "$MANAGED_POOL"
    (( APPLY )) && ok "Удалён пустой pool ${MANAGED_POOL}"
}

remove_local_project_state() {
    do_cmd rm -f -- /usr/local/sbin/pve-bootstrap-status /usr/local/sbin/deploy-guest
    do_cmd rm -rf -- "$CONFIG_DIR" "$RUNTIME_DIR" "$LOG_DIR" "$BOOTSTRAP_LOG_DIR"

    if id "$DEPLOY_USER" >/dev/null 2>&1; then
        if command -v pgrep >/dev/null 2>&1 && pgrep -u "$DEPLOY_USER" >/dev/null 2>&1; then
            warn "У ${DEPLOY_USER} есть работающие процессы; Linux user оставлен, чтобы не убивать процессы автоматически"
        else
            do_cmd userdel -r "$DEPLOY_USER"
            (( APPLY )) && ok "Удалён Linux user ${DEPLOY_USER}"
        fi
    else
        ok "Linux user ${DEPLOY_USER} уже отсутствует"
    fi

    if getent group "$DEPLOY_USER" >/dev/null 2>&1; then
        if id "$DEPLOY_USER" >/dev/null 2>&1; then
            warn "Группа ${DEPLOY_USER} оставлена, потому что одноимённый user не был удалён"
        else
            do_cmd groupdel "$DEPLOY_USER"
            (( APPLY )) && ok "Удалена Linux group ${DEPLOY_USER}"
        fi
    else
        ok "Linux group ${DEPLOY_USER} уже отсутствует"
    fi
}

find_oldest_bootstrap_storage_snapshot() {
    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
        | sort \
        | while IFS= read -r dir; do
            if [[ -f "${BACKUP_ROOT}/${dir}/files/etc/pve/storage.cfg" ]]; then
                printf '%s\n' "${BACKUP_ROOT}/${dir}/files/etc/pve/storage.cfg"
                break
            fi
        done
}

storage_content_from_cfg() {
    local file=$1 storage=$2
    awk -v wanted="$storage" '
        /^[^[:space:]][^:]*:[[:space:]]+/ {
            active = ($2 == wanted)
            next
        }
        active && /^[[:space:]]+content[[:space:]]+/ {
            print $2
            exit
        }
    ' "$file"
}

csv_has() {
    local csv=$1 item=$2
    tr ',' '\n' <<<"$csv" | grep -Fxq "$item"
}

restore_storage_baseline() {
    (( RESTORE_STORAGE_BASELINE )) || return

    local baseline storage baseline_content current_content
    baseline="$(find_oldest_bootstrap_storage_snapshot || true)"
    [[ -n "$baseline" ]] \
        || die "Не найден ранний bootstrap snapshot storage.cfg; baseline storage не изменяется"

    info "Storage baseline: ${baseline}"

    for storage in local local-lvm; do
        baseline_content="$(storage_content_from_cfg "$baseline" "$storage")"
        current_content="$(pvesh get "/storage/${storage}" --output-format json | jq -r '.content // empty')"

        [[ -n "$baseline_content" ]] \
            || die "В baseline не удалось определить content для storage ${storage}"

        if [[ "$storage" == "local-lvm" ]] && ! csv_has "$baseline_content" images; then
            die "Baseline local-lvm не содержит images; такой откат может скрыть VM disks и запрещён"
        fi

        if [[ "$storage" == "local-lvm" ]] && ! csv_has "$baseline_content" rootdir; then
            if pct list | awk 'NR>1 {found=1} END {exit !found}'; then
                warn "Есть LXC-контейнеры, а baseline local-lvm не содержит rootdir; local-lvm content оставлен без изменений"
                continue
            fi
        fi

        if [[ "$current_content" == "$baseline_content" ]]; then
            ok "Storage ${storage} уже соответствует baseline content=${baseline_content}"
            continue
        fi

        do_cmd pvesm set "$storage" --content "$baseline_content"
        (( APPLY )) && ok "Storage ${storage}: content восстановлен к baseline (${baseline_content})"
    done
}

verify_post_reset() {
    (( APPLY )) || return

    qm config "$TEMPLATE_VMID" >/dev/null 2>&1 \
        && die "После reset VMID ${TEMPLATE_VMID} всё ещё существует"

    pve_user_exists "$HOST_PVE_USER" \
        && die "После reset PVE user ${HOST_PVE_USER} всё ещё существует"
    pve_user_exists "$AI_PVE_USER" \
        && die "После reset PVE user ${AI_PVE_USER} всё ещё существует"

    [[ ! -e "$CONFIG_DIR" ]] || die "После reset ${CONFIG_DIR} всё ещё существует"
    [[ ! -e "$RUNTIME_DIR" ]] || die "После reset ${RUNTIME_DIR} всё ещё существует"

    if (( ! DROP_DEPLOY_KEY )) && [[ -f "$STAGE0_KEY" ]]; then
        ok "Stage 0 Deploy Key сохранён для следующего тестового запуска"
    elif (( ! DROP_DEPLOY_KEY )); then
        warn "Stage 0 Deploy Key не сохранён; следующий запуск может запросить регистрацию нового ключа"
    fi

    if qm config 100 >/dev/null 2>&1; then
        ok "VM 100 существует и не затрагивалась reset-скриптом"
    else
        info "VM 100 отсутствовала/отсутствует; reset-скрипт VMID 100 не изменяет"
    fi
}

create_reset_backup
seed_stage0_deploy_key

destroy_project_template
remove_template_caches

remove_pve_identity "$HOST_PVE_USER" "$HOST_PVE_TOKEN_NAME" "$HOST_PVE_TOKEN"
remove_pve_identity "$AI_PVE_USER" "$AI_PVE_TOKEN_NAME" "$AI_PVE_TOKEN"
remove_project_roles
remove_managed_pool_if_empty

restore_storage_baseline
remove_local_project_state
verify_post_reset

printf '\n============================================================\n'
if (( APPLY )); then
    printf 'ТЕСТОВЫЙ RESET ЗАВЕРШЁН\n'
    printf 'Основные bootstrap-артефакты удалены; посторонние гости не удалялись.\n'
    if (( DROP_DEPLOY_KEY )); then
        printf 'Следующий Stage 0 создаст новый GitHub Deploy Key.\n'
    else
        printf 'Текущий GitHub Deploy Key подготовлен во временной области Stage 0.\n'
    fi
    printf '\nДля повторного полного теста запустите:\n'
    printf '  curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/init-pve.sh | bash\n'
else
    printf 'DRY-RUN ЗАВЕРШЁН — НИЧЕГО НЕ ИЗМЕНЕНО\n'
    printf 'Проверьте план выше и повторите с --apply.\n'
fi
printf '============================================================\n'
