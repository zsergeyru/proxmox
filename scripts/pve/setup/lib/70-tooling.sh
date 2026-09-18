#!/usr/bin/env bash

install_private_tooling() {
    log "Установка стабильных локальных команд PVE"

    cat >/usr/local/sbin/pve-configuration-status <<'EOF_STATUS'
#!/usr/bin/env bash
set -Eeuo pipefail

STATE=/var/lib/proxmox-deployer/state/state.json
SMOKE=/var/lib/proxmox-deployer/state/template-smoke.json
LAST_REVISION=/var/lib/proxmox-deployer/state/last-revision
CONFIG=/etc/proxmox-deployer/config.yaml
SSH_DIR=/etc/proxmox-deployer/ssh
SECRETS_DIR=/etc/proxmox-deployer/secrets
RUNTIME_DIR=/var/lib/proxmox-deployer
REPO_DIR=/var/lib/proxmox-deployer/repo
PUBLIC_KEYS_DIR=/var/lib/proxmox-deployer/public-keys
DEPLOY_USER=pvedeploy
EXPECTED_REPO='git@github.com:zsergeyru/proxmox.git'
TEMPLATE_VMID=9000
TEMPLATE_NAME=tpl-debian13
MANAGED_POOL=managed

show_saved_status() {
    [[ -f "$STATE" ]] || { echo "Файл состояния PVE Configuration не найден: $STATE" >&2; exit 1; }
    printf '%s\n' '=== PVE Configuration: сохранённое состояние ==='
    jq . "$STATE"
    if [[ -f "$SMOKE" ]]; then
        printf '\n%s\n' '=== Проверка полного клона шаблона ==='
        jq . "$SMOKE"
    fi
}

usage() {
    cat <<'USAGE'
Использование:
  pve-configuration-status
  pve-configuration-status --check

Без параметров:
  показывает последнее сохранённое состояние PVE Configuration.

--check:
  заново проверяет фактическое состояние PVE-хоста;
  ничего не исправляет и не изменяет.

Коды возврата --check:
  0  все обязательные проверки пройдены
  1  обнаружена ошибка или несоответствие
  2  обязательные проверки пройдены, но есть предупреждения
USAGE
}

ERRORS=0
WARNINGS=0

COLOR_ENABLED=0
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != "dumb" ]]; then
    COLOR_ENABLED=1
fi

if (( COLOR_ENABLED )); then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""
    C_BOLD=""
    C_GREEN=""
    C_YELLOW=""
    C_RED=""
    C_CYAN=""
fi

check_ok() { printf '%s%s[ОК]%s %s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$1"; }
check_warn() { WARNINGS=$((WARNINGS + 1)); printf '%s%s[ПРЕДУПРЕЖДЕНИЕ]%s %s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET" "$1"; }
check_error() { ERRORS=$((ERRORS + 1)); printf '%s%s[ОШИБКА]%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$1"; }

check_file_contract() {
    local path=$1 owner_group=$2 mode=$3 label=$4 actual_owner actual_mode
    if [[ ! -f "$path" || -L "$path" ]]; then check_error "$label: отсутствует обычный файл $path"; return; fi
    actual_owner="$(stat -c '%U:%G' "$path" 2>/dev/null || true)"
    actual_mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
    if [[ "$actual_owner" == "$owner_group" && "$actual_mode" == "$mode" ]]; then
        check_ok "$label: $path"
    else
        check_error "$label: $path, ожидается $owner_group $mode, обнаружено ${actual_owner:-?} ${actual_mode:-?}"
    fi
}

check_dir_contract() {
    local path=$1 owner_group=$2 mode=$3 label=$4 actual_owner actual_mode
    if [[ ! -d "$path" || -L "$path" ]]; then check_error "$label: отсутствует каталог $path"; return; fi
    actual_owner="$(stat -c '%U:%G' "$path" 2>/dev/null || true)"
    actual_mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
    if [[ "$actual_owner" == "$owner_group" && "$actual_mode" == "$mode" ]]; then
        check_ok "$label: $path"
    else
        check_error "$label: $path, ожидается $owner_group $mode, обнаружено ${actual_owner:-?} ${actual_mode:-?}"
    fi
}

check_required_command() {
    local cmd=$1
    command -v "$cmd" >/dev/null 2>&1 && check_ok "команда установлена: $cmd" || check_error "не найдена обязательная команда: $cmd"
}

check_role() {
    local role=$1 required=$2 roles_json actual missing priv
    roles_json="$(pveum role list --output-format json 2>/dev/null || true)"
    actual="$(jq -r --arg role "$role" '.[] | select(.roleid == $role) | (.privs // "")' <<<"$roles_json" 2>/dev/null | head -n1)"
    if [[ -z "$actual" ]]; then check_error "роль Proxmox отсутствует: $role"; return; fi
    missing=""
    for priv in $required; do
        if ! tr ', ' '\n\n' <<<"$actual" | grep -Fxq "$priv"; then missing+="${priv} "; fi
    done
    [[ -z "$missing" ]] && check_ok "роль Proxmox: $role" || check_error "роль $role не содержит обязательные привилегии: ${missing% }"
}

check_token_permissions() {
    local user=$1 token=$2 label=$3 scope=$4 required=$5 json priv
    json="$(pveum user token permissions "$user" "$token" --output-format json 2>/dev/null || true)"
    if [[ -z "$json" ]] || ! jq -e . >/dev/null 2>&1 <<<"$json"; then check_error "$label: не удалось получить фактические права"; return; fi
    for priv in $required; do
        if ! jq -e --arg path "$scope" --arg priv "$priv" '((.[$path] // {}) | has($priv))' <<<"$json" >/dev/null; then
            check_error "$label: на $scope отсутствует $priv"
            return
        fi
    done
    check_ok "$label: права на $scope"
}

check_token_auth() {
    local token_id=$1 secret_file=$2 label=$3 stored_id secret response

    if [[ ! -f "$secret_file" || -L "$secret_file" ]]; then
        check_error "$label: файл секрета отсутствует или небезопасен"
        return
    fi

    stored_id="$(sed -n 's/^token_id=//p' "$secret_file" | head -n1)"
    secret="$(sed -n 's/^token_secret=//p' "$secret_file" | head -n1)"
    if [[ "$stored_id" != "$token_id" || -z "$secret" ]]; then
        check_error "$label: локальный secret не соответствует ожидаемому token_id"
        return
    fi

    if response="$(curl --fail --silent --show-error --insecure         --connect-timeout 10 --max-time 20         --header "Authorization: PVEAPIToken=${token_id}=${secret}"         https://127.0.0.1:8006/api2/json/version 2>/dev/null)"         && jq -e '(.data.version // .data.release // empty) != ""' <<<"$response" >/dev/null 2>&1; then
        check_ok "$label: локальная авторизация API работает"
    else
        check_error "$label: локальная авторизация API не прошла"
    fi
    unset secret response
}

run_check() {
    local saved_status user_record uid gid_name home shell guest_fp reg_fp parent_owner parent_mode violation origin drift revision recorded state_revision
    local users_json host_token ai_token pools_json storage template_cfg smoke_status

    printf '%s%s=== PVE Configuration: фактическая проверка ===%s\n' "$C_BOLD" "$C_CYAN" "$C_RESET"

    for cmd in jq git find stat getent id ssh-keygen curl pveversion pveum pvesm qm; do
        command -v "$cmd" >/dev/null 2>&1 || check_error "не найдена команда, необходимая для проверки: $cmd"
    done
    if (( ERRORS > 0 )); then printf '\n[ИТОГ] ERROR: errors=%d warnings=%d\n' "$ERRORS" "$WARNINGS"; return 1; fi

    pveversion >/dev/null 2>&1 && check_ok "Proxmox VE доступен" || check_error "не удалось получить версию Proxmox VE"

    if [[ -f "$STATE" ]] && jq -e '.component == "pve-configuration"' "$STATE" >/dev/null 2>&1; then
        check_ok "машинное состояние PVE Configuration читается"
        saved_status="$(jq -r '.status // "unknown"' "$STATE")"
        [[ "$saved_status" == "ready" || "$saved_status" == "ready-with-warnings" ]] || check_warn "последний сохранённый статус: $saved_status"
    else
        check_error "машинное состояние отсутствует или повреждено: $STATE"
    fi

    check_dir_contract /etc/proxmox-deployer root:root 755 "каталог конфигурации"
    check_dir_contract "$SSH_DIR" root:pvedeploy 750 "каталог SSH"
    check_dir_contract "$SECRETS_DIR" root:pvedeploy 710 "каталог секретов"
    check_dir_contract "$RUNTIME_DIR" root:pvedeploy 750 "каталог служебного состояния"
    check_dir_contract /var/lib/proxmox-deployer/state root:pvedeploy 750 "каталог состояния"
    check_dir_contract /var/lib/proxmox-deployer/cache pvedeploy:pvedeploy 750 "каталог кэша"
    check_dir_contract "$PUBLIC_KEYS_DIR" pvedeploy:pvedeploy 750 "реестр открытых управляющих ключей"
    check_dir_contract /var/lib/pvedeploy/.ssh pvedeploy:pvedeploy 700 "SSH-каталог pvedeploy"
    check_dir_contract /var/log/proxmox-deployer root:pvedeploy 750 "каталог журналов"

    if getent passwd "$DEPLOY_USER" >/dev/null 2>&1; then
        user_record="$(getent passwd "$DEPLOY_USER")"
        uid="$(cut -d: -f3 <<<"$user_record")"
        gid_name="$(id -gn "$DEPLOY_USER" 2>/dev/null || true)"
        home="$(cut -d: -f6 <<<"$user_record")"
        shell="$(cut -d: -f7 <<<"$user_record")"
        if (( uid < 1000 )) && [[ "$gid_name" == "$DEPLOY_USER" && "$home" == "/var/lib/pvedeploy" && "$shell" == "/bin/bash" ]]; then
            check_ok "пользователь pvedeploy соответствует контракту"
        else
            check_error "пользователь pvedeploy не соответствует контракту: uid=$uid group=$gid_name home=$home shell=$shell"
        fi
    else
        check_error "пользователь pvedeploy отсутствует"
    fi

    check_file_contract "$SSH_DIR/github_proxmox_repo_ed25519" root:root 600 "ключ чтения GitHub"
    check_file_contract "$SSH_DIR/github_proxmox_repo_ed25519.pub" root:root 644 "открытая часть ключа GitHub"
    check_file_contract "$SSH_DIR/config" root:root 600 "конфигурация SSH GitHub"
    check_file_contract "$SSH_DIR/known_hosts" root:root 644 "доверие SSH GitHub"
    check_file_contract "$SSH_DIR/pve_guest_ed25519" pvedeploy:pvedeploy 600 "закрытый ключ управления гостями"
    check_file_contract "$SSH_DIR/pve_guest_ed25519.pub" pvedeploy:pvedeploy 644 "открытый ключ управления гостями"
    check_file_contract "$SECRETS_DIR/host-deploy.token" root:pvedeploy 640 "секрет host-deploy"
    check_file_contract "$SECRETS_DIR/ai-agent-infra.token" root:root 600 "секрет ai-agent"
    check_file_contract "$PUBLIC_KEYS_DIR/deployer.pub" pvedeploy:pvedeploy 644 "deployer.pub"
    check_file_contract "$PUBLIC_KEYS_DIR/management-authorized-keys" pvedeploy:pvedeploy 644 "management-authorized-keys"

    if [[ -f "$SSH_DIR/pve_guest_ed25519.pub" && -f "$PUBLIC_KEYS_DIR/deployer.pub" ]]; then
        guest_fp="$(ssh-keygen -lf "$SSH_DIR/pve_guest_ed25519.pub" -E sha256 2>/dev/null | awk 'NR==1 {print $2}')"
        reg_fp="$(ssh-keygen -lf "$PUBLIC_KEYS_DIR/deployer.pub" -E sha256 2>/dev/null | awk 'NR==1 {print $2}')"
        [[ -n "$guest_fp" && "$guest_fp" == "$reg_fp" ]] && check_ok "deployer.pub соответствует PVE guest SSH identity" || check_error "deployer.pub не соответствует PVE guest SSH identity"
    fi

    if [[ -f "$CONFIG" ]]         && grep -Fxq 'repo: zsergeyru/proxmox' "$CONFIG"         && grep -Fxq 'branch: main' "$CONFIG"         && grep -Fxq 'checkout: /var/lib/proxmox-deployer/repo' "$CONFIG"         && grep -Fxq 'managed_pool: managed' "$CONFIG"         && grep -Fxq 'host_deploy_identity: deployer@pve!host-deploy' "$CONFIG"         && grep -Fxq 'ai_infra_identity: ai-agent@pve!infra' "$CONFIG"         && grep -Fxq 'template_vmid: 9000' "$CONFIG"; then
        check_ok "локальная конфигурация соответствует контракту"
    else
        check_error "локальная конфигурация отсутствует или не соответствует ожидаемой модели: $CONFIG"
    fi

    if [[ -d "$REPO_DIR/.git" ]]; then
        parent_owner="$(stat -c '%U:%G' "$RUNTIME_DIR" 2>/dev/null || true)"
        parent_mode="$(stat -c '%a' "$RUNTIME_DIR" 2>/dev/null || true)"
        violation="$(find "$REPO_DIR" -xdev \( -type f -o -type d \) \( ! -uid 0 -o -perm /022 \) -print -quit 2>/dev/null || true)"
        origin="$(git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
        drift="$(git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" status --porcelain=v1 --untracked-files=all --ignored 2>/dev/null || true)"
        drift="$(printf '%s\n' "$drift" | grep -Ev '^!! .*(__pycache__/|\.py[co]$)' || true)"
        revision="$(git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
        recorded="$(cat "$LAST_REVISION" 2>/dev/null || true)"
        state_revision="$(jq -r '.repository_revision // empty' "$STATE" 2>/dev/null || true)"

        [[ "$parent_owner" == "root:pvedeploy" && "$parent_mode" == "750" ]] && check_ok "родитель доверенной копии защищён" || check_error "родитель доверенной копии имеет неверные владельца или права"
        [[ -z "$violation" ]] && check_ok "доверенная копия принадлежит root и защищена от записи" || check_error "нарушена граница доверия Git: ${violation:-неизвестно}"
        [[ "$origin" == "$EXPECTED_REPO" ]] && check_ok "origin доверенной копии соответствует проекту" || check_error "неожиданный origin: ${origin:-не задан}"
        [[ -z "$drift" ]] && check_ok "локальные изменения в доверенной копии отсутствуют" || check_error "в доверенной копии есть локальные изменения: $(head -n1 <<<"$drift")"

        if [[ "$revision" =~ ^[0-9a-f]{40}$ && "$revision" == "$recorded" && "$revision" == "$state_revision" ]]; then
            check_ok "ревизия Git согласована со служебным состоянием"
        else
            check_error "ревизия Git не согласована: git=${revision:-?} last-revision=${recorded:-?} state=${state_revision:-?}"
        fi
    else
        check_error "доверенная локальная копия проекта отсутствует: $REPO_DIR"
    fi

    users_json="$(pveum user list --output-format json 2>/dev/null || true)"
    for user in deployer@pve ai-agent@pve; do
        jq -e --arg id "$user" '.[] | select(.userid == $id and ((.enable // 1) != 0))' <<<"$users_json" >/dev/null 2>&1             && check_ok "пользователь Proxmox: $user" || check_error "пользователь Proxmox отсутствует или отключён: $user"
    done

    host_token="$(pveum user token list deployer@pve --output-format json 2>/dev/null || true)"
    ai_token="$(pveum user token list ai-agent@pve --output-format json 2>/dev/null || true)"
    jq -e '.[] | select(.tokenid == "host-deploy" and ((.privsep // 1) == 1))' <<<"$host_token" >/dev/null 2>&1         && check_ok "API-токен deployer@pve!host-deploy" || check_error "API-токен deployer@pve!host-deploy отсутствует или имеет privsep != 1"
    jq -e '.[] | select(.tokenid == "infra" and ((.privsep // 1) == 1))' <<<"$ai_token" >/dev/null 2>&1         && check_ok "API-токен ai-agent@pve!infra" || check_error "API-токен ai-agent@pve!infra отсутствует или имеет privsep != 1"

    check_role AICloneSource 'VM.Audit VM.Clone'
    check_role AIManagedGuest 'VM.Allocate VM.Audit VM.Backup VM.Clone VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.Console VM.GuestAgent.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback'
    check_role AINetworkUse 'SDN.Use'
    check_role AIStorage 'Datastore.AllocateSpace Datastore.Audit'
    check_role AIManagedPool 'Pool.Allocate Pool.Audit'

    pools_json="$(pveum pool list --output-format json 2>/dev/null || true)"
    jq -e --arg p "$MANAGED_POOL" '.[] | select(.poolid == $p)' <<<"$pools_json" >/dev/null 2>&1         && check_ok "пул Proxmox: $MANAGED_POOL" || check_error "пул Proxmox отсутствует: $MANAGED_POOL"

    check_token_permissions deployer@pve host-deploy "deployer@pve!host-deploy" /vms 'VM.Allocate VM.Audit VM.Backup VM.Clone VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.Console VM.GuestAgent.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback'
    check_token_permissions deployer@pve host-deploy "deployer@pve!host-deploy" /pool/managed 'Pool.Allocate Pool.Audit'
    check_token_permissions deployer@pve host-deploy "deployer@pve!host-deploy" /storage/local 'Datastore.AllocateSpace Datastore.Audit'
    check_token_permissions deployer@pve host-deploy "deployer@pve!host-deploy" /storage/local-lvm 'Datastore.AllocateSpace Datastore.Audit'
    check_token_permissions deployer@pve host-deploy "deployer@pve!host-deploy" /sdn/zones/localnetwork/vmbr0 'SDN.Use'

    check_token_permissions ai-agent@pve infra "ai-agent@pve!infra" /pool/managed 'VM.Allocate VM.Audit VM.Backup VM.Clone VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.Console VM.GuestAgent.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback Pool.Allocate Pool.Audit'
    check_token_permissions ai-agent@pve infra "ai-agent@pve!infra" /vms/9000 'VM.Audit VM.Clone'
    check_token_permissions ai-agent@pve infra "ai-agent@pve!infra" /storage/local 'Datastore.AllocateSpace Datastore.Audit'
    check_token_permissions ai-agent@pve infra "ai-agent@pve!infra" /storage/local-lvm 'Datastore.AllocateSpace Datastore.Audit'
    check_token_permissions ai-agent@pve infra "ai-agent@pve!infra" /sdn/zones/localnetwork/vmbr0 'SDN.Use'

    check_token_auth 'deployer@pve!host-deploy' "$SECRETS_DIR/host-deploy.token" "deployer@pve!host-deploy"
    check_token_auth 'ai-agent@pve!infra' "$SECRETS_DIR/ai-agent-infra.token" "ai-agent@pve!infra"

    for storage in local local-lvm; do
        pvesm status --storage "$storage" >/dev/null 2>&1 && check_ok "хранилище доступно: $storage" || check_error "хранилище недоступно: $storage"
    done

    if template_cfg="$(qm config "$TEMPLATE_VMID" 2>/dev/null)"; then
        if grep -Fxq 'template: 1' <<<"$template_cfg" && grep -Fxq "name: $TEMPLATE_NAME" <<<"$template_cfg"; then
            check_ok "базовый шаблон VMID $TEMPLATE_VMID соответствует основным признакам"
        else
            check_error "VMID $TEMPLATE_VMID существует, но не соответствует базовому шаблону $TEMPLATE_NAME"
        fi
    else
        check_error "базовый шаблон VMID $TEMPLATE_VMID отсутствует"
    fi

    check_required_command pve-configuration-status
    check_required_command deploy-guest
    check_required_command sync-management-keys

    if [[ -f "$SMOKE" ]]; then
        smoke_status="$(jq -r '.status // .result // "unknown"' "$SMOKE" 2>/dev/null || true)"
        case "$smoke_status" in
            passed|success) check_ok "последняя проверка полного клона шаблона: $smoke_status" ;;
            pending|unknown|"") check_warn "состояние проверки полного клона шаблона: ${smoke_status:-unknown}" ;;
            *) check_error "проверка полного клона шаблона имеет состояние: $smoke_status" ;;
        esac
    else
        check_warn "файл состояния проверки полного клона шаблона отсутствует"
    fi

    printf '\n'
    if (( ERRORS > 0 )); then
        printf '%s%s[ИТОГ] ERROR%s: errors=%d warnings=%d\n' "$C_BOLD" "$C_RED" "$C_RESET" "$ERRORS" "$WARNINGS"
        return 1
    fi
    if (( WARNINGS > 0 )); then
        printf '%s%s[ИТОГ] PARTIAL%s: errors=0 warnings=%d\n' "$C_BOLD" "$C_YELLOW" "$C_RESET" "$WARNINGS"
        return 2
    fi
    printf '%s%s[ИТОГ] OK%s: фактическое состояние соответствует обязательным проверкам\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
}

case "${1:-}" in
    "") show_saved_status ;;
    --check) [[ $# -eq 1 ]] || { usage >&2; exit 1; }; run_check ;;
    -h|--help) usage ;;
    *) usage >&2; exit 1 ;;
esac
EOF_STATUS
    chmod 0755 /usr/local/sbin/pve-configuration-status

    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"
    if [[ ! -f "$deployer_src" ]]; then
        if [[ -e /usr/local/sbin/deploy-guest ]]; then
            rm -f /usr/local/sbin/deploy-guest
            warn "Исходный файл deploy-guest отсутствует: ${deployer_src}. Старая локальная команда удалена."
        else
            warn "Исходный файл deploy-guest пока отсутствует: ${deployer_src}"
        fi
        return
    fi

    cat >/usr/local/sbin/deploy-guest <<EOF_DEPLOY
#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${REPO_DIR}"
RUNTIME_DIR="${RUNTIME_DIR}"
STATE_DIR="${STATE_DIR}"
PRIVATE_REPO="${PRIVATE_REPO}"
PRIVATE_BRANCH="${PRIVATE_BRANCH}"
SSH_CONFIG="${SSH_CONFIG}"
PROJECT_REPO_KEY="${KEY_FILE}"
DEPLOY_USER="${DEPLOY_USER}"
LOCK_FILE="${LOCK_FILE}"
SOURCE="${deployer_src}"
VALIDATOR="${REPO_DIR}/scripts/validate_repo.py"

fail() {
    printf 'ОШИБКА: %s\\n' "\$*" >&2
    exit 1
}

[[ \$EUID -eq 0 ]] || fail "deploy-guest нужно запускать только от root"

for cmd in git ssh flock runuser find stat; do
    command -v "\$cmd" >/dev/null 2>&1 || fail "Не найдена обязательная команда: \$cmd"
done

[[ -d "\$REPO_DIR/.git" ]] || fail "Каноническая копия Git отсутствует: \$REPO_DIR. Сначала выполните PVE Configuration."
[[ -r "\$SSH_CONFIG" ]] || fail "Не найден root-only SSH config для GitHub: \$SSH_CONFIG"

exec 9>"\$LOCK_FILE"
flock -n 9 || fail "Другой Public Bootstrap, PVE Configuration или deploy-guest уже выполняется"

canonical_git() {
    env GIT_SSH_COMMAND="ssh -F \$SSH_CONFIG" \\
        git -c "safe.directory=\$REPO_DIR" "\$@"
}

assert_repo_trust() {
    local violation parent_owner parent_mode
    parent_owner="\$(stat -c '%U:%G' "\$RUNTIME_DIR" 2>/dev/null || true)"
    parent_mode="\$(stat -c '%a' "\$RUNTIME_DIR" 2>/dev/null || true)"
    [[ "\$parent_owner" == "root:\$DEPLOY_USER" && "\$parent_mode" == "750" ]] \\
        || fail "\$RUNTIME_DIR должен быть root:\$DEPLOY_USER 0750, обнаружено \${parent_owner:-?} \${parent_mode:-?}"

    violation="\$(find "\$REPO_DIR" -xdev \\( -type f -o -type d \\) \\( ! -uid 0 -o -perm /022 \\) -print -quit 2>/dev/null || true)"
    [[ -z "\$violation" ]] \\
        || fail "Каноническая копия Git не защищена: \$violation не принадлежит root или доступен на запись группе/остальным"
}

assert_clean_repo() {
    local status
    status="\$(canonical_git -C "\$REPO_DIR" status --porcelain=v1 --untracked-files=all --ignored)" \\
        || fail "Не удалось проверить состояние Git в \$REPO_DIR"
    status="\$(printf '%s\n' "\$status" | grep -Ev '^!! .*(__pycache__/|\.py[co]$)' || true)"
    [[ -z "\$status" ]] \\
        || fail "В \$REPO_DIR есть локальные изменения. Автоматическое удаление запрещено. Первый элемент: \$(head -n1 <<<"\$status")"
}

resolve_project_repo_read() {
    local vmid=\$1
    PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache /usr/bin/python3 - "\$REPO_DIR" "\$vmid" <<'PY_EFFECTIVE'
from pathlib import Path
import sys
import yaml

repo = Path(sys.argv[1])
vmid = int(sys.argv[2])
sys.path.insert(0, str(repo / "scripts"))

from guest_config import GuestConfigError, resolve_effective_guest

matches = sorted((repo / "guests").glob(f"{vmid:03d}-*/guest.yaml"))
if len(matches) != 1:
    raise SystemExit(f"для VMID {vmid} ожидается ровно один guest.yaml, найдено: {len(matches)}")

try:
    defaults = yaml.safe_load((repo / "guests" / "defaults.yaml").read_text(encoding="utf-8"))
    source = yaml.safe_load(matches[0].read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"не удалось прочитать effective state для VMID {vmid}: {exc}") from exc

if not isinstance(defaults, dict) or not isinstance(source, dict):
    raise SystemExit("defaults.yaml и guest.yaml должны содержать YAML mapping")
if source.get("vmid") != vmid:
    raise SystemExit(f"guest.yaml не соответствует запрошенному VMID {vmid}")
if source.get("deployable") is not True:
    raise SystemExit(f"VMID {vmid} не является deployable")

try:
    effective = resolve_effective_guest(source, defaults).effective
except GuestConfigError as exc:
    raise SystemExit(f"не удалось построить effective state VMID {vmid}: {exc}") from exc

management = effective.get("management")
if not isinstance(management, dict):
    raise SystemExit("effective management должен быть mapping")
value = management.get("project_repo_read")
if not isinstance(value, bool):
    raise SystemExit("effective management.project_repo_read должен быть boolean")
print("1" if value else "0")
PY_EFFECTIVE
}

assert_repo_trust

origin_url="\$(canonical_git -C "\$REPO_DIR" remote get-url origin 2>/dev/null || true)"
[[ "\$origin_url" == "\$PRIVATE_REPO" ]] \\
    || fail "Неожиданный адрес Git: \${origin_url:-не задан}. Ожидается \$PRIVATE_REPO"

assert_clean_repo

current_revision="\$(canonical_git -C "\$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
recorded_revision="\$(cat "\$STATE_DIR/last-revision" 2>/dev/null || true)"
[[ "\$current_revision" =~ ^[0-9a-f]{40}\$ ]] || fail "Не удалось определить текущую версию Git"
[[ "\$recorded_revision" =~ ^[0-9a-f]{40}\$ ]] \\
    || fail "Не найдена сохранённая версия Git в \$STATE_DIR/last-revision. Сначала выполните PVE Configuration."
[[ "\$current_revision" == "\$recorded_revision" ]] \\
    || fail "Текущая версия Git отличается от сохранённой версии. Автоматическое переключение запрещено."

printf 'Обновление Git %s...\\n' "\$PRIVATE_BRANCH"
canonical_git -C "\$REPO_DIR" fetch --depth 1 origin "\$PRIVATE_BRANCH"
revision="\$(canonical_git -C "\$REPO_DIR" rev-parse FETCH_HEAD 2>/dev/null || true)"
[[ "\$revision" =~ ^[0-9a-f]{40}\$ ]] || fail "Не удалось определить полученную версию Git"

if [[ "\$current_revision" != "\$revision" ]]; then
    canonical_git -C "\$REPO_DIR" reset --hard "\$revision"
    canonical_git -C "\$REPO_DIR" clean -ffd
fi

chmod -R go-w "\$REPO_DIR"
assert_repo_trust
assert_clean_repo

final_revision="\$(canonical_git -C "\$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
[[ "\$final_revision" == "\$revision" ]] \\
    || fail "После обновления Git ожидалась версия \$revision, получена \$final_revision"

revision_tmp="\$(mktemp "\$STATE_DIR/.last-revision.deploy.XXXXXX")"
printf '%s\\n' "\$revision" >"\$revision_tmp"
chown root:"\$DEPLOY_USER" "\$revision_tmp"
chmod 0640 "\$revision_tmp"
mv -f "\$revision_tmp" "\$STATE_DIR/last-revision"

[[ -f "\$SOURCE" ]] || fail "После обновления Git не найден основной файл deploy-guest: \$SOURCE"
[[ -f "\$VALIDATOR" ]] || fail "После обновления Git не найден validator: \$VALIDATOR"
[[ -f "\$REPO_DIR/scripts/guest_config.py" ]] || fail "После обновления Git не найден общий guest resolver"

PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache /usr/bin/python3 "\$VALIDATOR" || fail "Repository validation перед deploy-guest завершилась ошибкой"

vmid="\${1:-}"
[[ "\$vmid" =~ ^[0-9]{3}\$ ]] || fail "Первым параметром должен быть VMID 100-999"
vmid_num=\$((10#\$vmid))
(( vmid_num >= 100 && vmid_num <= 999 )) || fail "VMID должен находиться в диапазоне 100-999"
project_repo_read="\$(resolve_project_repo_read "\$vmid_num")" \\
    || fail "Не удалось определить effective management.project_repo_read для VMID \$vmid"
[[ "\$project_repo_read" == "0" || "\$project_repo_read" == "1" ]] \\
    || fail "Resolver вернул недопустимое значение project_repo_read: \$project_repo_read"

printf 'Версия Git: %s\\n' "\$revision"

if [[ "\$project_repo_read" == "1" ]]; then
    [[ -f "\$PROJECT_REPO_KEY" && ! -L "\$PROJECT_REPO_KEY" ]] \\
        || fail "Project Git READ key отсутствует или не является обычным файлом: \$PROJECT_REPO_KEY"
    key_owner="\$(stat -c '%U:%G' "\$PROJECT_REPO_KEY" 2>/dev/null || true)"
    key_mode="\$(stat -c '%a' "\$PROJECT_REPO_KEY" 2>/dev/null || true)"
    [[ "\$key_owner" == "root:root" && "\$key_mode" == "600" ]] \\
        || fail "Project Git READ key должен быть root:root 0600, обнаружено \${key_owner:-?} \${key_mode:-?}"

    exec 8<"\$PROJECT_REPO_KEY"
    set +e
    runuser -u "\$DEPLOY_USER" -- env \\
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache \\
        DEPLOY_GUEST_SOURCE_REVISION="\$revision" \\
        DEPLOY_GUEST_PROJECT_REPO_KEY_FD=8 \\
        /usr/bin/python3 "\$SOURCE" "\$@"
    rc=\$?
    set -e
    exec 8<&-
else
    set +e
    runuser -u "\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1 \
        PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache \\
        DEPLOY_GUEST_SOURCE_REVISION="\$revision" \\
        /usr/bin/python3 "\$SOURCE" "\$@"
    rc=\$?
    set -e
fi

exit "\$rc"
EOF_DEPLOY
    chown root:root /usr/local/sbin/deploy-guest
    chmod 0700 /usr/local/sbin/deploy-guest
    ok "Установлена root-only обёртка /usr/local/sbin/deploy-guest"
}

report_status() {
    local ready=1 smoke_state deploy_owner deploy_mode
    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"

    printf '\n%s%sPVE CONFIGURATION STATUS%s\n\n' "$C_BOLD" "$C_CYAN" "$C_RESET"
    ok "Proxmox VE 9 / Debian trixie / KVM проверены"
    ok "Эксклюзивная блокировка PVE Configuration используется"
    ok "PVE/Ceph repository policy без subscription проверена"
    ok "DNS, GitHub HTTPS и доступ к PVE repository работают"
    ok "local/local-lvm готовы для images/rootdir/vztmpl/snippets"
    ok "Debian 13 LXC template подготовлен: ${LXC_TEMPLATE_VOLUME:-не определён}"
    ok "pvedeploy, config.yaml и файловая структура проверены"
    ok "PVE guest SSH identity создана/проверена"
    ok "Management public-key registry создан и проверен"
    ok "Канонический read-only Deploy Key установлен"
    ok "Приватный репозиторий синхронизирован и origin проверен"
    ok "managed и роли Proxmox проверены"
    ok "${HOST_PVE_TOKEN}: guest-level /vms, effective permissions и API credential проверены"
    ok "${AI_PVE_TOKEN}: managed-only effective permissions и API credential проверены"

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        ok "Шаблон ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION} существует и полный contract проверен"
    else
        printf '%s%s[НЕТ]%s Шаблон %s отсутствует\n' "$C_BOLD" "$C_RED" "$C_RESET" "$TEMPLATE_VMID"
        ready=0
    fi

    smoke_state="$(template_smoke_state_status)"
    case "$smoke_state" in
        passed)
            ok "Full Clone smoke-test template ${TEMPLATE_VMID} подтверждён; временный VMID ${SMOKE_VMID} освобождён"
            ;;
        pending)
            printf '%s%s[ОЖИДАНИЕ]%s Full Clone smoke-test template %s имеет pending state\n' \
                "$C_BOLD" "$C_YELLOW" "$C_RESET" "$TEMPLATE_VMID"
            ready=0
            ;;
        none)
            info "Full Clone smoke state для существующего template ещё не записан; для явной проверки используйте --smoke-test-template"
            ;;
        *)
            printf '%s%s[ОШИБКА]%s Неизвестный template smoke state: %s\n' \
                "$C_BOLD" "$C_RED" "$C_RESET" "$smoke_state"
            ready=0
            ;;
    esac

    if [[ -e /usr/local/sbin/deploy-guest ]]; then
        deploy_owner="$(stat -c '%U:%G' /usr/local/sbin/deploy-guest 2>/dev/null || true)"
        deploy_mode="$(stat -c '%a' /usr/local/sbin/deploy-guest 2>/dev/null || true)"
    else
        deploy_owner=""
        deploy_mode=""
    fi

    if [[ -x /usr/local/sbin/deploy-guest && -f "$deployer_src" \
        && "$deploy_owner" == "root:root" && "$deploy_mode" == "700" ]]; then
        ok "Команда deploy-guest установлена как root:root 0700 и source существует"
    elif [[ -e /usr/local/sbin/deploy-guest ]]; then
        printf '%s%s[ОШИБКА]%s deploy-guest имеет неверное состояние: owner=%s mode=%s source=%s\n' \
            "$C_BOLD" "$C_RED" "$C_RESET" "${deploy_owner:-?}" "${deploy_mode:-?}" "$deployer_src"
        ready=0
    else
        printf '%s%s[ОЖИДАНИЕ]%s deploy-guest пока не реализован\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
        ready=0
    fi

    printf '\nРевизия приватного репозитория: %s\n' "$REPO_REVISION"
    printf 'Версия PVE Configuration: %s\n' "$PVE_CONFIGURATION_VERSION"
    printf 'Предупреждений: %s\n' "$WARN_COUNT"

    if (( ready )); then
        if (( WARN_COUNT )); then
            printf '\n%s%sГОТОВО С ПРЕДУПРЕЖДЕНИЯМИ%s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
            write_state "ready-with-warnings" "$REPO_REVISION"
        else
            printf '\n%s%sГОТОВО%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
            write_state "ready" "$REPO_REVISION"
        fi
    else
        printf '\n%s%sЧАСТИЧНО:%s основная PVE Configuration выполнена, но один или несколько рабочих компонентов ещё не готовы.\n' "$C_BOLD" "$C_MAGENTA" "$C_RESET"
        write_state "partial" "$REPO_REVISION"
    fi
}