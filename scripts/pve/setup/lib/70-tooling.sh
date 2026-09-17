#!/usr/bin/env bash

install_private_tooling() {
    log "Установка стабильных локальных команд PVE"

    cat >/usr/local/sbin/pve-configuration-status <<'EOF_STATUS'
#!/usr/bin/env bash
set -Eeuo pipefail
STATE=/var/lib/proxmox-deployer/state/state.json
SMOKE=/var/lib/proxmox-deployer/state/template-smoke.json
[[ -f "$STATE" ]] || { echo "Файл состояния PVE Configuration не найден: $STATE" >&2; exit 1; }
printf '%s\n' '=== PVE Configuration ==='
jq . "$STATE"
if [[ -f "$SMOKE" ]]; then
    printf '\n%s\n' '=== Template Full Clone smoke ==='
    jq . "$SMOKE"
fi
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
    status="\$(canonical_git -C "\$REPO_DIR" status --porcelain=v1 --untracked-files=all)" \\
        || fail "Не удалось проверить состояние Git в \$REPO_DIR"
    [[ -z "\$status" ]] \\
        || fail "В \$REPO_DIR есть локальные изменения. Автоматическое удаление запрещено. Первый элемент: \$(head -n1 <<<"\$status")"
}

resolve_project_repo_read() {
    local vmid=\$1
    PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - "\$REPO_DIR" "\$vmid" <<'PY_EFFECTIVE'
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

PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "\$VALIDATOR" || fail "Repository validation перед deploy-guest завершилась ошибкой"

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
        PYTHONDONTWRITEBYTECODE=1 \\
        DEPLOY_GUEST_SOURCE_REVISION="\$revision" \\
        DEPLOY_GUEST_PROJECT_REPO_KEY_FD=8 \\
        /usr/bin/python3 "\$SOURCE" "\$@"
    rc=\$?
    set -e
    exec 8<&-
else
    set +e
    runuser -u "\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1 \\
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
