#!/usr/bin/env bash

validate_config_yaml() {
    local file="$CONFIG_DIR/config.yaml"

    python3 - "$file" \
        "zsergeyru/proxmox" "$PRIVATE_BRANCH" "$REPO_DIR" "$MANAGED_POOL" \
        "$HOST_PVE_TOKEN" "$AI_PVE_TOKEN" "$TEMPLATE_VMID" <<'PY_CONFIG'
import sys
import yaml

path = sys.argv[1]
expected = {
    "repo": sys.argv[2],
    "branch": sys.argv[3],
    "checkout": sys.argv[4],
    "managed_pool": sys.argv[5],
    "host_deploy_identity": sys.argv[6],
    "ai_infra_identity": sys.argv[7],
    "template_vmid": sys.argv[8],
}

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
except Exception as exc:
    print(f"Не удалось прочитать {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(data, dict):
    print(f"{path} должен содержать YAML mapping", file=sys.stderr)
    raise SystemExit(1)

errors = []
for key, expected_value in expected.items():
    if key not in data:
        errors.append(f"отсутствует ключ {key}")
        continue
    actual = data[key]
    if str(actual) != str(expected_value):
        errors.append(f"{key}: ожидается {expected_value!r}, обнаружено {actual!r}")

if errors:
    print(f"{path} не соответствует текущей PVE Configuration:", file=sys.stderr)
    for item in errors:
        print(f"  - {item}", file=sys.stderr)
    raise SystemExit(1)
PY_CONFIG
}

validate_deploy_user_contract() {
    local entry uid gid home shell primary_group
    entry="$(getent passwd "$DEPLOY_USER" || true)"
    [[ -n "$entry" ]] || die "Linux-пользователь ${DEPLOY_USER} отсутствует после подготовки runtime"

    IFS=: read -r _ _ uid gid _ home shell <<<"$entry"
    primary_group="$(getent group "$gid" | awk -F: 'NR==1 {print $1}')"

    [[ "$uid" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить UID пользователя ${DEPLOY_USER}"
    (( uid < 1000 )) \
        || die "Существующий ${DEPLOY_USER} имеет UID=${uid}; ожидается system user с UID < 1000. Автоматическое изменение существующего пользователя запрещено."
    [[ "$primary_group" == "$DEPLOY_USER" ]] \
        || die "Существующий ${DEPLOY_USER} имеет primary group '${primary_group:-не определена}', ожидается '${DEPLOY_USER}'. Автоматическое изменение запрещено."
    [[ "$home" == "/var/lib/pvedeploy" ]] \
        || die "Существующий ${DEPLOY_USER} имеет home '${home}', ожидается '/var/lib/pvedeploy'. Автоматическое изменение запрещено."
    [[ "$shell" == "/bin/bash" ]] \
        || die "Существующий ${DEPLOY_USER} имеет shell '${shell}', ожидается '/bin/bash'. Автоматическое изменение запрещено."

    ok "Linux-пользователь ${DEPLOY_USER} соответствует runtime contract"
}

ensure_runtime_layout() {
    log "Подготовка каталогов и локального пользователя pvedeploy"

    if ! getent group "$DEPLOY_USER" >/dev/null 2>&1; then
        groupadd --system "$DEPLOY_USER"
        ok "Создана Linux-группа ${DEPLOY_USER}"
    fi

    if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
        useradd --system --gid "$DEPLOY_USER" --create-home \
            --home-dir /var/lib/pvedeploy --shell /bin/bash "$DEPLOY_USER"
        ok "Создан Linux-пользователь ${DEPLOY_USER}"
    else
        ok "Linux-пользователь ${DEPLOY_USER} уже существует"
    fi

    validate_deploy_user_contract

    install -d -o root -g root -m 0755 "$CONFIG_DIR"
    install -d -o root -g "$DEPLOY_USER" -m 0750 "$SSH_DIR"
    install -d -o root -g "$DEPLOY_USER" -m 0710 "$SECRETS_DIR"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 \
        "$RUNTIME_DIR" "$RUNTIME_DIR/state" "$RUNTIME_DIR/cache"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$LOG_DIR" "$LOG_DIR/audit"
    install -d -o root -g root -m 0700 "$BACKUP_ROOT" "$SECRETS_BACKUP_ROOT"
    install -d -o root -g root -m 0755 /usr/local/sbin

    if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
        cat >"$CONFIG_DIR/config.yaml" <<EOF_CONFIG
repo: zsergeyru/proxmox
branch: ${PRIVATE_BRANCH}
checkout: ${REPO_DIR}
managed_pool: ${MANAGED_POOL}
host_deploy_identity: ${HOST_PVE_TOKEN}
ai_infra_identity: ${AI_PVE_TOKEN}
template_vmid: ${TEMPLATE_VMID}
EOF_CONFIG
        chmod 0644 "$CONFIG_DIR/config.yaml"
        ok "Создан ${CONFIG_DIR}/config.yaml"
    else
        ok "Файл ${CONFIG_DIR}/config.yaml уже существует и не перезаписывается"
    fi

    validate_config_yaml \
        || die "Существующий ${CONFIG_DIR}/config.yaml конфликтует с текущей моделью PVE Configuration. Автоматическая перезапись запрещена."
    ok "${CONFIG_DIR}/config.yaml соответствует текущей PVE Configuration"
}

ensure_pve_guest_key() {
    log "Проверка постоянной PVE guest SSH identity"

    if [[ ! -f "$PVE_GUEST_KEY" ]]; then
        [[ ! -e "$PVE_GUEST_PUB" ]] \
            || die "Private key ${PVE_GUEST_KEY} отсутствует, но ${PVE_GUEST_PUB} существует. Возможна потеря PVE guest credential; автоматическая ротация запрещена. Восстановите private key из backup либо выполните отдельную осознанную rotation operation."

        local old_umask
        old_umask="$(umask)"
        umask 077
        if ! ssh-keygen -q -t ed25519 -N '' -C 'pve-guest' -f "$PVE_GUEST_KEY"; then
            umask "$old_umask"
            die "Не удалось создать PVE guest SSH keypair"
        fi
        umask "$old_umask"
        ok "Создан новый PVE guest SSH private key"
    else
        ok "PVE guest SSH private key уже существует и не ротируется"
    fi

    local derived_pub tmp_pub
    derived_pub="$(ssh-keygen -y -f "$PVE_GUEST_KEY" 2>/dev/null)" \
        || die "Не удалось прочитать PVE guest private key ${PVE_GUEST_KEY}"
    [[ "$derived_pub" == ssh-ed25519\ * ]] \
        || die "PVE guest key ${PVE_GUEST_KEY} должен быть Ed25519"

    chown "$DEPLOY_USER:$DEPLOY_USER" "$PVE_GUEST_KEY"
    chmod 0600 "$PVE_GUEST_KEY"

    tmp_pub="$(mktemp "${SSH_DIR}/.pve-guest-pub.XXXXXX")"
    printf '%s %s\n' "$derived_pub" 'pve-guest' >"$tmp_pub"
    install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$tmp_pub" "$PVE_GUEST_PUB"
    rm -f "$tmp_pub"

    [[ -s "$PVE_GUEST_PUB" ]] || die "PVE guest public key не создан: ${PVE_GUEST_PUB}"
    ok "PVE guest SSH identity проверена: ${PVE_GUEST_KEY}"
}

refresh_canonical_public_key() {
    local derived_pub tmp_pub
    derived_pub="$(ssh-keygen -y -f "$KEY_FILE" 2>/dev/null)" \
        || die "Не удалось прочитать canonical Deploy Key ${KEY_FILE}"
    [[ -n "$derived_pub" ]] || die "Из canonical Deploy Key не удалось получить public key"

    tmp_pub="$(mktemp "${SSH_DIR}/.github-key-pub.XXXXXX")"
    printf '%s %s\n' "$derived_pub" 'pve-canonical-readonly-zsergeyru-proxmox' >"$tmp_pub"
    install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$tmp_pub" "${KEY_FILE}.pub"
    rm -f "$tmp_pub"
}

prepare_canonical_github_access() {
    log "Принятие read-only Deploy Key из Public Bootstrap"

    if [[ ! -f "$KEY_FILE" ]]; then
        [[ -f "$BOOTSTRAP_KEY_FILE" ]] \
            || die "Канонический GitHub Deploy Key отсутствует и Public Bootstrap не передал временный private key. Сначала запустите публичный proxmox-bootstrap/bootstrap-pve.sh."

        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 "$BOOTSTRAP_KEY_FILE" "$KEY_FILE"
        ok "Deploy Key перенесён в каноническое хранилище"
    else
        chown "$DEPLOY_USER:$DEPLOY_USER" "$KEY_FILE"
        chmod 0600 "$KEY_FILE"
        ok "Канонический GitHub Deploy Key уже существует"
    fi

    refresh_canonical_public_key

    if [[ -f "$BOOTSTRAP_KEY_FILE" ]]; then
        local canonical_pub bootstrap_pub
        canonical_pub="$(ssh-keygen -y -f "$KEY_FILE" 2>/dev/null || true)"
        bootstrap_pub="$(ssh-keygen -y -f "$BOOTSTRAP_KEY_FILE" 2>/dev/null || true)"
        [[ -n "$bootstrap_pub" ]] || die "Public Bootstrap передал нечитаемый private Deploy Key: ${BOOTSTRAP_KEY_FILE}"
        if [[ "$canonical_pub" != "$bootstrap_pub" ]]; then
            CANONICAL_KEY_DIFFERS_FROM_BOOTSTRAP=1
            warn "Public Bootstrap использует другой Deploy Key, чем уже существующий canonical key. PVE Configuration не заменяет постоянный credential автоматически."
        fi
    fi

    if [[ -f "$BOOTSTRAP_KNOWN_HOSTS" ]]; then
        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$BOOTSTRAP_KNOWN_HOSTS" "$KNOWN_HOSTS"
    elif [[ ! -f "$KNOWN_HOSTS" ]]; then
        local tmp_hosts
        tmp_hosts="$(mktemp)"
        curl -fsSL --connect-timeout 10 --max-time 20 https://api.github.com/meta \
            | jq -r '.ssh_keys[] | "github.com " + .' >"$tmp_hosts"
        [[ -s "$tmp_hosts" ]] || {
            rm -f "$tmp_hosts"
            die "Не удалось получить SSH-ключи хоста GitHub через api.github.com/meta"
        }
        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$tmp_hosts" "$KNOWN_HOSTS"
        rm -f "$tmp_hosts"
    fi

    cat >"$SSH_CONFIG" <<EOF_SSH
Host github.com
    HostName github.com
    User git
    IdentityFile ${KEY_FILE}
    IdentitiesOnly yes
    UserKnownHostsFile ${KNOWN_HOSTS}
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 10
    ServerAliveInterval 15
    ServerAliveCountMax 2
EOF_SSH
    chown "$DEPLOY_USER:$DEPLOY_USER" "$SSH_CONFIG"
    chmod 0600 "$SSH_CONFIG"
}

git_as_deployer() {
    runuser -u "$DEPLOY_USER" -- env GIT_SSH_COMMAND="ssh -F ${SSH_CONFIG}" git "$@"
}

verify_private_repo_access() {
    local refs
    if ! refs="$(git_as_deployer ls-remote "$PRIVATE_REPO" "refs/heads/${PRIVATE_BRANCH}" 2>/dev/null)"; then
        if (( CANONICAL_KEY_DIFFERS_FROM_BOOTSTRAP )); then
            die "Canonical Deploy Key ${KEY_FILE} не даёт доступ к ${PRIVATE_REPO}, при этом Public Bootstrap использует другой ключ. Автоматическая замена постоянного ключа запрещена. Проверьте ${KEY_FILE}.pub, Deploy keys GitHub и явно выполните recovery/rotation canonical credential."
        fi
        die "Канонический Deploy Key не даёт read-only доступ к ${PRIVATE_REPO}"
    fi
    [[ -n "$refs" ]] \
        || die "Канонический Deploy Key работает, но в ${PRIVATE_REPO} отсутствует ожидаемая ветка ${PRIVATE_BRANCH}"
    ok "Read-only доступ к приватному GitHub-репозиторию и ветке ${PRIVATE_BRANCH} подтверждён"
}

configuration_source_revision() {
    local revision="${PVE_CONFIGURATION_SOURCE_REVISION:-}"
    local source_root

    if [[ -z "$revision" ]]; then
        source_root="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
        revision="$(git -C "$source_root" rev-parse HEAD 2>/dev/null || true)"
    fi

    [[ "$revision" =~ ^[0-9a-f]{40}$ ]] \
        || die "Не удалось определить точную Git revision, из которой запущена PVE Configuration"
    printf '%s\n' "$revision"
}

assert_clean_git_worktree() {
    local repo=$1 label=$2 status
    status="$(git_as_deployer -C "$repo" status --porcelain=v1 --untracked-files=all)" \
        || die "Не удалось проверить чистоту ${label} Git checkout ${repo}"
    [[ -z "$status" ]] \
        || die "${label} Git checkout ${repo} содержит локальные изменения или untracked-файлы. Автоматический reset/clean запрещён, чтобы не потерять данные. Первый элемент: $(head -n1 <<<"$status")"
}

sync_private_repo() {
    log "Фиксация canonical private checkout на revision текущей PVE Configuration"

    local expected_revision origin_url current_revision fetched_revision
    expected_revision="$(configuration_source_revision)"

    if [[ ! -d "$REPO_DIR/.git" ]]; then
        rm -rf "$REPO_DIR"
        git_as_deployer clone --depth 1 --branch "$PRIVATE_BRANCH" "$PRIVATE_REPO" "$REPO_DIR"
        current_revision="$(git_as_deployer -C "$REPO_DIR" rev-parse HEAD)"
        [[ "$current_revision" == "$expected_revision" ]] \
            || die "Во время первого canonical clone ветка ${PRIVATE_BRANCH} изменилась: PVE Configuration запущена из ${expected_revision}, а clone получил ${current_revision}. Ничего из новой revision не применяется; повторите Public Bootstrap."
        assert_clean_git_worktree "$REPO_DIR" "Canonical"
    else
        chown -R "$DEPLOY_USER:$DEPLOY_USER" "$REPO_DIR"
        origin_url="$(git_as_deployer -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
        [[ "$origin_url" == "$PRIVATE_REPO" ]] \
            || die "Существующий checkout ${REPO_DIR} имеет неожиданный origin '${origin_url:-не задан}'. Ожидается '${PRIVATE_REPO}'. Автоматическая подмена origin запрещена."
        ok "Origin существующего private checkout соответствует каноническому репозиторию"

        assert_clean_git_worktree "$REPO_DIR" "Canonical"
        current_revision="$(git_as_deployer -C "$REPO_DIR" rev-parse HEAD)"
        if [[ "$current_revision" != "$expected_revision" ]]; then
            git_as_deployer -C "$REPO_DIR" fetch --depth 1 origin "$PRIVATE_BRANCH"
            fetched_revision="$(git_as_deployer -C "$REPO_DIR" rev-parse FETCH_HEAD)"
            [[ "$fetched_revision" == "$expected_revision" ]] \
                || die "Ветка ${PRIVATE_BRANCH} изменилась во время PVE Configuration: ожидается ${expected_revision}, fetch получил ${fetched_revision}. Canonical checkout не переключён; повторите Public Bootstrap."
            git_as_deployer -C "$REPO_DIR" reset --hard "$expected_revision"
            git_as_deployer -C "$REPO_DIR" clean -ffd
            assert_clean_git_worktree "$REPO_DIR" "Canonical"
        else
            ok "Canonical checkout уже находится на revision текущей PVE Configuration и не имеет локального drift"
        fi
    fi

    REPO_REVISION="$(git_as_deployer -C "$REPO_DIR" rev-parse HEAD)"
    [[ "$REPO_REVISION" == "$expected_revision" ]] \
        || die "Canonical checkout ${REPO_DIR} имеет revision ${REPO_REVISION}, ожидается ${expected_revision}"

    printf '%s\n' "$REPO_REVISION" >"$RUNTIME_DIR/state/last-revision"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$RUNTIME_DIR/state/last-revision"
    ok "Canonical private checkout зафиксирован на revision: ${REPO_REVISION}"
}
