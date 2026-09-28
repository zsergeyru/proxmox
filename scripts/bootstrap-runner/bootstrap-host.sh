#!/usr/bin/env bash
set -Eeuo pipefail

BOOTSTRAP_HOST_VERSION="1.0.0-dev1"

CTID="${BOOTSTRAP_RUNNER_CTID:-990}"
CT_HOSTNAME="bootstrap-runner"
PROJECT_BRANCH="${PROJECT_BRANCH:-feature/bootstrap-990}"
PROJECT_DIR="${PROJECT_DIR:-/var/lib/bootstrap-runner/project}"

HOST_BOOTSTRAP_DIR="${HOST_BOOTSTRAP_DIR:-/root/.config/proxmox-bootstrap}"
HOST_GITHUB_KEY="${HOST_GITHUB_KEY:-$HOST_BOOTSTRAP_DIR/github_proxmox_repo_ed25519}"
HOST_TEMPLATE_MARKER="${HOST_TEMPLATE_MARKER:-$HOST_BOOTSTRAP_DIR/debian13-template.ref}"
HOST_LOG_FILE="${HOST_LOG_FILE:-/var/log/proxmox-bootstrap.log}"

INFRA_CTID=910
INFRA_HOSTNAME="infra-manager"
INFRA_PROJECT_DIR="/var/lib/infra-manager/bootstrap-repo"
INFRA_GITHUB_KEY="/root/.ssh/github_proxmox_repo_ed25519"
INFRA_GITHUB_CONFIG="/root/.ssh/github_config"
INFRA_GITHUB_KNOWN_HOSTS="/root/.ssh/github_known_hosts"
INFRA_STAGING_SECRET="/root/.infra-manager-bootstrap/pve-api.env"

MODE="apply"

C_RESET=""
C_BOLD=""
C_GREEN=""
C_BLUE=""
C_YELLOW=""
C_RED=""
C_CYAN=""
BOOTSTRAP_COLOR=0

if [[ "${NO_COLOR:-}" == "" && "${TERM:-}" != "dumb" ]]; then
    printf -v C_RESET '\033[0m'
    printf -v C_BOLD '\033[1m'
    printf -v C_GREEN '\033[32m'
    printf -v C_BLUE '\033[34m'
    printf -v C_YELLOW '\033[33m'
    printf -v C_RED '\033[31m'
    printf -v C_CYAN '\033[36m'
    BOOTSTRAP_COLOR=1
fi

log()  { printf '\n%s%s==> %s%s\n' "$C_BOLD" "$C_BLUE" "$*" "$C_RESET"; }
ok()   { printf '%s%s[ОК]%s %s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$*"; }
info() { printf '%s%s[ИНФО]%s %s\n' "$C_BOLD" "$C_CYAN" "$C_RESET" "$*"; }
die()  { printf '\n%s%sОШИБКА:%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Использование:
  bootstrap-host.sh [--check|--recover|--remove|--purge]

Режимы:
  без параметров    создать 910 либо обновить существующий 910
  --check           проверить готовность 910
  --recover         восстановить постоянный PVE API-доступ и повторить настройку
  --remove          удалить 910 и временный контур
  --purge           то же самое и удалить постоянный GitHub Deploy Key
USAGE
}

parse_args() {
    while (($#)); do
        case "$1" in
            --check)
                [[ "$MODE" == "apply" ]] || die "можно выбрать только один режим"
                MODE="check"
                ;;
            --recover)
                [[ "$MODE" == "apply" ]] || die "можно выбрать только один режим"
                MODE="recover"
                ;;
            --remove)
                [[ "$MODE" == "apply" ]] || die "можно выбрать только один режим"
                MODE="remove"
                ;;
            --purge)
                [[ "$MODE" == "apply" ]] || die "можно выбрать только один режим"
                MODE="purge"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "неизвестный параметр закрытого bootstrap: $1"
                ;;
        esac
        shift
    done
}

require_host() {
    [[ $EUID -eq 0 ]] || die "сценарий должен выполняться от root на PVE"
    for command in pct pveum pvesm perl ssh-keyscan; do
        command -v "$command" >/dev/null 2>&1 || die "не найден $command"
    done
    [[ -s "$HOST_GITHUB_KEY" ]] || die "отсутствует GitHub Deploy Key: $HOST_GITHUB_KEY"
}

run_quiet() {
    local rc=0
    "$@" >>"$HOST_LOG_FILE" 2>&1 || rc=$?
    if ((rc != 0)); then
        printf '%sПоследние строки технического журнала:%s\n' "$C_YELLOW" "$C_RESET" >&2
        tail -n 30 "$HOST_LOG_FILE" >&2 || true
        printf 'Полный журнал: %s\n' "$HOST_LOG_FILE" >&2
        return "$rc"
    fi
}

ct_exists() { pct config "$CTID" >/dev/null 2>&1; }
ct_exec() { pct exec "$CTID" -- "$@"; }
ct_run_quiet() { run_quiet pct exec "$CTID" -- "$@"; }
infra_exec() { pct exec "$INFRA_CTID" -- "$@"; }
infra_run_quiet() { run_quiet pct exec "$INFRA_CTID" -- "$@"; }

assert_owned_ct() {
    local config
    ct_exists || return 1
    config="$(pct config "$CTID")"
    grep -Fxq "hostname: $CT_HOSTNAME" <<<"$config" || return 1
    grep -Eq '^tags: .*bootstrap-runner' <<<"$config" || return 1
    grep -Eq '^description: .*managed-by=proxmox-bootstrap' <<<"$config" || return 1
}

verify_runner_contract() {
    assert_owned_ct || die "LXC $CTID отсутствует или не принадлежит bootstrap"
    [[ "$(pct status "$CTID" | awk '{print $2}')" == "running" ]] || die "LXC $CTID должен быть запущен"
    ct_exec test -d "$PROJECT_DIR/.git" || die "в LXC $CTID отсутствует закрытый репозиторий"
    ct_exec test -s "$PROJECT_DIR/scripts/bootstrap-runner/pve-access.sh" || die "в проекте отсутствует pve-access.sh"
    ct_exec test -s "$PROJECT_DIR/scripts/bootstrap-runner/prepare-runtime.sh" || die "в проекте отсутствует prepare-runtime.sh"
    ct_exec test -s "$PROJECT_DIR/scripts/bootstrap-runner/deploy-910.sh" || die "в проекте отсутствует deploy-910.sh"
}

run_private_host_access() {
    local helper
    helper="$(mktemp /run/bootstrap-runner-pve-access.XXXXXX)"
    pct pull "$CTID" "$PROJECT_DIR/scripts/bootstrap-runner/pve-access.sh" "$helper"
    chmod 0700 "$helper"
    run_quiet env BOOTSTRAP_RUNNER_MODE=apply BOOTSTRAP_RUNNER_CTID="$CTID" bash "$helper"
    rm -f "$helper"
}

prepare_runtime() {
    log "Подготовка среды bootstrap-runner"
    ct_run_quiet bash "$PROJECT_DIR/scripts/bootstrap-runner/prepare-runtime.sh" "$PROJECT_DIR"
    ok "Среда bootstrap-runner готова"
}

create_infra_manager_infrastructure() {
    log "Создание LXC 910 через OpenTofu"
    ct_run_quiet env INFRA_PROJECT_BRANCH="$PROJECT_BRANCH"         bash "$PROJECT_DIR/scripts/bootstrap-runner/deploy-910.sh" infrastructure "$PROJECT_DIR"
    ok "LXC 910 создан через состояние bootstrap-runner"
}

prepare_infra_manager_base() {
    log "Базовая настройка LXC 910 через Ansible"
    ct_run_quiet env INFRA_PROJECT_BRANCH="$PROJECT_BRANCH"         bash "$PROJECT_DIR/scripts/bootstrap-runner/deploy-910.sh" base "$PROJECT_DIR"
    ok "Базовая настройка 910 завершена"
}

provision_infra_manager() {
    log "Полная настройка LXC 910 через Ansible"
    ct_run_quiet env INFRA_PROJECT_BRANCH="$PROJECT_BRANCH"         bash "$PROJECT_DIR/scripts/bootstrap-runner/deploy-910.sh" provision "$PROJECT_DIR"
    ok "Полная настройка 910 завершена"
}

provision_existing_infra_manager() {
    log "Обновление существующего LXC 910"
    ct_run_quiet env INFRA_PROJECT_BRANCH="$PROJECT_BRANCH"         bash "$PROJECT_DIR/scripts/bootstrap-runner/deploy-910.sh" existing "$PROJECT_DIR"
    ok "Существующий 910 обновлён"
}

infra_manager_exists() {
    pct config "$INFRA_CTID" >/dev/null 2>&1
}

ensure_existing_infra_manager_running() {
    local config status
    infra_manager_exists || return 1
    config="$(pct config "$INFRA_CTID")"
    grep -Fxq "hostname: $INFRA_HOSTNAME" <<<"$config" || die "VMID $INFRA_CTID занят чужим LXC"
    status="$(pct status "$INFRA_CTID" | awk '{print $2}')"
    [[ "$status" == "running" ]] || pct start "$INFRA_CTID"
}

verify_infra_manager_object() {
    local config status
    pct config "$INFRA_CTID" >/dev/null 2>&1 || die "LXC $INFRA_CTID отсутствует"
    config="$(pct config "$INFRA_CTID")"
    grep -Fxq "hostname: $INFRA_HOSTNAME" <<<"$config" || die "VMID $INFRA_CTID не является infra-manager"
    status="$(pct status "$INFRA_CTID" | awk '{print $2}')"
    [[ "$status" == "running" ]] || die "LXC $INFRA_CTID должен быть запущен"
}

prepare_infra_manager_pve_access() {
    local access_mode="${1:-apply}" helper
    verify_infra_manager_object

    helper="$(mktemp /run/infra-manager-pve-access.XXXXXX)"
    pct pull "$CTID" "$PROJECT_DIR/scripts/infra-manager/pve-bootstrap-access.sh" "$helper"
    chmod 0700 "$helper"

    run_quiet env         INFRA_MANAGER_CTID="$INFRA_CTID"         INFRA_MANAGER_MODE="$access_mode"         INFRA_MANAGER_COLOR="$BOOTSTRAP_COLOR"         INFRA_MANAGER_SECRET_FILE="$INFRA_STAGING_SECRET"         bash "$helper"
    rm -f "$helper"

    if ! infra_exec test -s "$INFRA_STAGING_SECRET"         && ! infra_exec test -s /etc/infra-manager/secrets/pve-api.env; then
        die "PVE API credential не передан в 910"
    fi
    ok "Постоянный PVE API-доступ 910 подготовлен"
}

prepare_infra_manager_project_access() {
    local known_hosts
    verify_infra_manager_object

    infra_exec install -d -m 0700 /root/.ssh
    pct push "$INFRA_CTID" "$HOST_GITHUB_KEY" "$INFRA_GITHUB_KEY"         --user 0 --group 0 --perms 0600

    known_hosts="$(mktemp /run/infra-manager-known-hosts.XXXXXX)"
    ssh-keyscan -t ed25519 github.com >"$known_hosts" 2>/dev/null
    pct push "$INFRA_CTID" "$known_hosts" "$INFRA_GITHUB_KNOWN_HOSTS"         --user 0 --group 0 --perms 0644
    rm -f "$known_hosts"

    infra_exec sh -c 'cat >"$1" <<EOF
Host github.com
    HostName github.com
    User git
    IdentityFile /root/.ssh/github_proxmox_repo_ed25519
    IdentitiesOnly yes
    UserKnownHostsFile /root/.ssh/github_known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 10
EOF
chmod 0600 "$1"
' sh "$INFRA_GITHUB_CONFIG"
    ok "GitHub-доступ передан в 910"
}

checkout_infra_manager_project() {
    local git_ssh="ssh -F $INFRA_GITHUB_CONFIG"

    if infra_exec test -d "$INFRA_PROJECT_DIR/.git"; then
        infra_run_quiet env GIT_SSH_COMMAND="$git_ssh"             git -C "$INFRA_PROJECT_DIR" fetch --depth 1 origin "$PROJECT_BRANCH"
        infra_run_quiet git -C "$INFRA_PROJECT_DIR" reset --hard FETCH_HEAD
        infra_run_quiet git -C "$INFRA_PROJECT_DIR" clean -ffdx
    else
        infra_exec rm -rf "$INFRA_PROJECT_DIR"
        infra_exec install -d -m 0755 "$(dirname "$INFRA_PROJECT_DIR")"
        infra_run_quiet env GIT_SSH_COMMAND="$git_ssh"             git clone --depth 1 --branch "$PROJECT_BRANCH"             git@github.com:zsergeyru/proxmox.git "$INFRA_PROJECT_DIR"
    fi
    ok "Закрытый проект передан в 910"
}

verify_infra_manager_handoff() {
    verify_infra_manager_object
    if ! infra_exec test -s "$INFRA_STAGING_SECRET"         && ! infra_exec test -s /etc/infra-manager/secrets/pve-api.env; then
        die "в 910 отсутствует PVE API credential"
    fi
    infra_exec test -s /usr/local/share/ca-certificates/pve-root-ca.crt || die "в 910 отсутствует PVE CA"
    infra_exec test -s "$INFRA_GITHUB_KEY" || die "в 910 отсутствует GitHub Deploy Key"
    infra_exec test -d "$INFRA_PROJECT_DIR/.git" || die "в 910 отсутствует рабочая копия проекта"
    ok "Данные для настройки 910 переданы"
}

handoff_infra_manager() {
    local access_mode="${1:-apply}"
    prepare_infra_manager_pve_access "$access_mode"
    prepare_infra_manager_project_access
    checkout_infra_manager_project
    verify_infra_manager_handoff
}

handoff_existing_infra_manager() {
    prepare_infra_manager_project_access
    checkout_infra_manager_project
    infra_exec test -s /etc/infra-manager/secrets/pve-api.env         || die "в существующем 910 отсутствует постоянный PVE API credential"
}

verify_infra_manager_ready() {
    verify_infra_manager_object
    infra_exec test -x /usr/local/sbin/infra-manager-status         || die "в 910 отсутствует infra-manager-status"
    infra_exec env         INFRA_PROJECT_BRANCH="$PROJECT_BRANCH"         INFRA_MANAGER_COLOR="$BOOTSTRAP_COLOR"         /usr/local/sbin/infra-manager-status --full
    ok "910 infra-manager готов по полному контракту"
}

temporary_token_exists() {
    pveum user token list root@pam --output-format json 2>/dev/null         | perl -MJSON::PP -0777 -e '
            my $rows = decode_json(<STDIN>);
            exit((grep { (($_->{tokenid} // q{}) eq q{bootstrap-runner}) } @$rows) ? 0 : 1);
        '
}

remove_token_acls_by_id() {
    local token_id=$1 entry path role
    while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        path=${entry%%|*}
        role=${entry#*|}
        pveum acl delete "$path" --tokens "$token_id" --roles "$role" || true
    done < <(
        pveum acl list --output-format json             | perl -MJSON::PP -0777 -e '
                my $token = shift;
                my $rows = decode_json(<STDIN>);
                for my $row (@$rows) {
                    next unless (($row->{type} // q{}) eq q{token});
                    next unless (($row->{ugid} // q{}) eq $token);
                    print(($row->{path} // q{}), q{|}, ($row->{roleid} // q{}), qq{\n});
                }
            ' "$token_id"
    )
}

remove_named_token() {
    local user=$1 token_name=$2 token_id
    token_id="$user!$token_name"
    remove_token_acls_by_id "$token_id"
    if pveum user token list "$user" --output-format json 2>/dev/null         | perl -MJSON::PP -0777 -e '
            my $token = shift;
            my $rows = decode_json(<STDIN>);
            exit((grep { (($_->{tokenid} // q{}) eq $token) } @$rows) ? 0 : 1);
        ' "$token_name"; then
        pveum user token remove "$user" "$token_name"
    fi
}

remove_private_access() {
    local helper
    if assert_owned_ct && ct_exec test -f "$PROJECT_DIR/scripts/bootstrap-runner/pve-access.sh"; then
        helper="$(mktemp /run/bootstrap-runner-pve-access-remove.XXXXXX)"
        pct pull "$CTID" "$PROJECT_DIR/scripts/bootstrap-runner/pve-access.sh" "$helper"
        chmod 0700 "$helper"
        run_quiet env BOOTSTRAP_RUNNER_MODE=remove BOOTSTRAP_RUNNER_CTID="$CTID" bash "$helper"
        rm -f "$helper"
    else
        remove_named_token "root@pam" "bootstrap-runner"
    fi
    temporary_token_exists && die "временный PVE API token 990 не удалён"
}

remove_downloaded_template() {
    local ref volume
    [[ -s "$HOST_TEMPLATE_MARKER" ]] || return 0
    ref="$(cat "$HOST_TEMPLATE_MARKER")"
    if pvesm path "$ref" >/dev/null 2>&1; then
        volume="${ref#*:}"
        pvesm free "$ref" >/dev/null 2>&1 || rm -f -- "$(pvesm path "$ref")"
        info "Удалён временно скачанный LXC-шаблон: $volume"
    fi
    rm -f "$HOST_TEMPLATE_MARKER"
}

finalize_bootstrap_runner() {
    assert_owned_ct || die "невозможно завершить bootstrap: LXC 990 отсутствует"

    ct_exec rm -rf /etc/bootstrap-runner/secrets /var/lib/bootstrap-runner/opentofu/state
    ct_exec test ! -e /etc/bootstrap-runner/secrets || die "временные секреты 990 не удалены"
    ct_exec test ! -e /var/lib/bootstrap-runner/opentofu/state || die "временное состояние 990 не удалено"

    remove_private_access

    if [[ "$(pct status "$CTID" | awk '{print $2}')" == "running" ]]; then
        pct stop "$CTID"
    fi
    run_quiet pct destroy "$CTID" --purge 1
    ct_exists && die "LXC 990 не удалён"
    remove_downloaded_template
    ok "Временный контур 990 полностью удалён"
}

remove_bootstrap_runner_if_present() {
    if ct_exists; then
        assert_owned_ct || die "VMID $CTID занят чужим объектом"
        remove_private_access
        if [[ "$(pct status "$CTID" | awk '{print $2}')" == "running" ]]; then
            pct stop "$CTID"
        fi
        run_quiet pct destroy "$CTID" --purge 1
    else
        remove_named_token "root@pam" "bootstrap-runner"
    fi
    remove_downloaded_template
}

remove_infra_manager() {
    remove_bootstrap_runner_if_present

    if infra_manager_exists; then
        local config
        config="$(pct config "$INFRA_CTID")"
        grep -Fxq "hostname: $INFRA_HOSTNAME" <<<"$config" || die "VMID $INFRA_CTID занят чужим объектом"
        remove_named_token "root@pam" "infra-manager"
        pct set "$INFRA_CTID" --protection 0 >/dev/null
        if [[ "$(pct status "$INFRA_CTID" | awk '{print $2}')" == "running" ]]; then
            pct stop "$INFRA_CTID"
        fi
        run_quiet pct destroy "$INFRA_CTID" --purge 1
        ok "LXC 910 удалён"
    else
        remove_named_token "root@pam" "infra-manager"
        ok "LXC 910 уже отсутствует"
    fi
}

check_ready() {
    infra_manager_exists || die "LXC 910 отсутствует"
    verify_infra_manager_ready
    ct_exists && die "после успешного bootstrap временный LXC 990 не должен существовать"
    temporary_token_exists && die "после успешного bootstrap временный token 990 не должен существовать"
    ok "Постоянный 910 готов, временный контур отсутствует"
}

prepare_runner() {
    verify_runner_contract
    run_private_host_access
    prepare_runtime
}

apply() {
    local existed=0 runner_owns_910=0
    infra_manager_exists && existed=1

    prepare_runner

    if ct_exec test -s /var/lib/bootstrap-runner/opentofu/state/proxmox.tfstate; then
        runner_owns_910=1
    fi

    if ((existed && runner_owns_910)); then
        info "Найден незавершённый первоначальный контур; продолжается его состояние"
        ensure_existing_infra_manager_running
        create_infra_manager_infrastructure
        prepare_infra_manager_base
        [[ "$MODE" == "recover" ]] && handoff_infra_manager recover || handoff_infra_manager
        provision_infra_manager
    elif ((existed)); then
        ensure_existing_infra_manager_running
        [[ "$MODE" == "recover" ]] && prepare_infra_manager_pve_access recover
        handoff_existing_infra_manager
        provision_existing_infra_manager
    else
        create_infra_manager_infrastructure
        prepare_infra_manager_base
        [[ "$MODE" == "recover" ]] && handoff_infra_manager recover || handoff_infra_manager
        provision_infra_manager
    fi

    verify_infra_manager_ready
    finalize_bootstrap_runner
    check_ready
}

main() {
    parse_args "$@"
    require_host
    verify_runner_contract
    info "Закрытый bootstrap $BOOTSTRAP_HOST_VERSION, режим: $MODE"

    case "$MODE" in
        apply|recover)
            apply
            ;;
        check)
            check_ready
            finalize_bootstrap_runner
            ;;
        remove)
            remove_infra_manager
            ;;
        purge)
            remove_infra_manager
            rm -rf "$HOST_BOOTSTRAP_DIR"
            ok "Постоянный GitHub Deploy Key удалён"
            ;;
        *)
            die "неизвестный режим: $MODE"
            ;;
    esac
}

main "$@"
