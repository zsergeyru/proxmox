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
    log "Подготовка root-trusted source и локального runtime пользователя pvedeploy"

    local deploy_ssh_dir="/var/lib/pvedeploy/.ssh"
    local guest_known_hosts="${deploy_ssh_dir}/known_hosts"

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

    # Родитель canonical repo не должен быть writable для pvedeploy: иначе даже
    # root-owned repo можно было бы удалить/переименовать и заменить целиком.
    install -d -o root -g "$DEPLOY_USER" -m 0750 "$RUNTIME_DIR" "$STATE_DIR"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$RUNTIME_DIR/cache"

    install -d -o root -g "$DEPLOY_USER" -m 0750 "$LOG_DIR"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$LOG_DIR/audit"
    install -d -o root -g root -m 0700 "$BACKUP_ROOT" "$SECRETS_BACKUP_ROOT"
    install -d -o root -g root -m 0755 /usr/local/sbin

    # pvedeploy не читает /etc/pve напрямую. Публичный PVE CA копируется
    # в root-owned runtime-файл с read-only доступом для pvedeploy.
    [[ -f "$PVE_CA_SOURCE" ]] \
        || die "Не найден исходный PVE CA: ${PVE_CA_SOURCE}"
    [[ ! -L "$PVE_CA_FILE" ]] \
        || die "Runtime PVE CA ${PVE_CA_FILE} является симлинком; автоматическое обновление запрещено"
    install -o root -g "$DEPLOY_USER" -m 0640 "$PVE_CA_SOURCE" "$PVE_CA_FILE"
    cmp -s "$PVE_CA_SOURCE" "$PVE_CA_FILE" \
        || die "Runtime-копия PVE CA не совпадает с ${PVE_CA_SOURCE}"
    runuser -u "$DEPLOY_USER" -- test -r "$PVE_CA_FILE" \
        || die "Runtime PVE CA недоступен для ${DEPLOY_USER}: ${PVE_CA_FILE}"
    ok "Публичный PVE CA подготовлен для ограниченного runtime: ${PVE_CA_FILE}"

    # deploy-guest хранит SSH host keys управляемых VM/LXC отдельно от
    # root-only known_hosts, используемого для GitHub. Не следуем симлинкам,
    # которые ограниченный пользователь мог подложить перед root-run.
    [[ ! -L "$deploy_ssh_dir" ]] \
        || die "${deploy_ssh_dir} является симлинком; автоматическая подготовка SSH runtime запрещена"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0700 "$deploy_ssh_dir"

    [[ ! -L "$guest_known_hosts" ]] \
        || die "${guest_known_hosts} является симлинком; автоматическое изменение запрещено"
    if [[ -e "$guest_known_hosts" && ! -f "$guest_known_hosts" ]]; then
        die "${guest_known_hosts} существует, но не является обычным файлом"
    fi
    if [[ ! -e "$guest_known_hosts" ]]; then
        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 /dev/null "$guest_known_hosts"
        ok "Создана отдельная база SSH host key гостевых систем: ${guest_known_hosts}"
    else
        chown "$DEPLOY_USER:$DEPLOY_USER" "$guest_known_hosts"
        chmod 0600 "$guest_known_hosts"
        ok "База SSH host key гостевых систем проверена: ${guest_known_hosts}"
    fi

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
    install -o root -g root -m 0644 "$tmp_pub" "${KEY_FILE}.pub"
    rm -f "$tmp_pub"
}

prepare_canonical_github_access() {
    log "Проверка постоянной root-only GitHub identity для canonical source"

    [[ -f "$KEY_FILE" ]] \
        || die "Постоянный GitHub Deploy Key ${KEY_FILE} отсутствует. Public Bootstrap должен создать его до запуска PVE Configuration; автоматическая генерация или ротация здесь запрещена."

    chown root:root "$KEY_FILE"
    chmod 0600 "$KEY_FILE"
    refresh_canonical_public_key

    if [[ ! -f "$KNOWN_HOSTS" ]]; then
        local tmp_hosts
        tmp_hosts="$(mktemp)"
        curl -fsSL --connect-timeout 10 --max-time 20 https://api.github.com/meta \
            | jq -r '.ssh_keys[] | "github.com " + .' >"$tmp_hosts"
        [[ -s "$tmp_hosts" ]] || {
            rm -f "$tmp_hosts"
            die "Не удалось получить SSH-ключи хоста GitHub через api.github.com/meta"
        }
        install -o root -g root -m 0644 "$tmp_hosts" "$KNOWN_HOSTS"
        rm -f "$tmp_hosts"
    else
        chown root:root "$KNOWN_HOSTS"
        chmod 0644 "$KNOWN_HOSTS"
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
    chown root:root "$SSH_CONFIG"
    chmod 0600 "$SSH_CONFIG"
}

canonical_git() {
    env GIT_SSH_COMMAND="ssh -F ${SSH_CONFIG}" \
        git -c "safe.directory=${REPO_DIR}" "$@"
}

verify_private_repo_access() {
    local refs
    refs="$(canonical_git ls-remote "$PRIVATE_REPO" "refs/heads/${PRIVATE_BRANCH}" 2>/dev/null)" \
        || die "Канонический root-only Deploy Key не даёт read-only доступ к ${PRIVATE_REPO}"
    [[ -n "$refs" ]] \
        || die "Канонический Deploy Key работает, но в ${PRIVATE_REPO} отсутствует ожидаемая ветка ${PRIVATE_BRANCH}"
    ok "Root-only read-only доступ к приватному GitHub-репозиторию и ветке ${PRIVATE_BRANCH} подтверждён"
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

harden_canonical_repo_permissions() {
    [[ -e "$REPO_DIR" ]] || return 0

    chown -R root:root "$REPO_DIR"
    chmod -R go-w "$REPO_DIR"
}

assert_canonical_repo_trust() {
    local violation parent_owner parent_mode

    parent_owner="$(stat -c '%U:%G' "$RUNTIME_DIR" 2>/dev/null || true)"
    parent_mode="$(stat -c '%a' "$RUNTIME_DIR" 2>/dev/null || true)"
    [[ "$parent_owner" == "root:${DEPLOY_USER}" && "$parent_mode" == "750" ]] \
        || die "${RUNTIME_DIR} должен быть root:${DEPLOY_USER} 0750, обнаружено ${parent_owner:-?} ${parent_mode:-?}; иначе pvedeploy может подменить canonical repo целиком"

    violation="$(find "$REPO_DIR" -xdev \( -type f -o -type d \) \( ! -uid 0 -o -perm /022 \) -print -quit 2>/dev/null || true)"
    [[ -z "$violation" ]] \
        || die "Canonical checkout не является root-trusted: '${violation}' не root-owned или доступен на запись группе/остальным"
}

assert_clean_git_worktree() {
    local repo=$1 label=$2 status
    status="$(canonical_git -C "$repo" status --porcelain=v1 --untracked-files=all --ignored)" \
        || die "Не удалось проверить чистоту ${label} Git checkout ${repo}"
    [[ -z "$status" ]] \
        || die "${label} Git checkout ${repo} содержит tracked/staged/untracked/ignored drift. Автоматический reset/clean запрещён, чтобы не потерять данные. Первый элемент: $(head -n1 <<<"$status")"
}

sync_private_repo() {
    log "Фиксация root-trusted canonical private checkout на revision текущей PVE Configuration"

    local expected_revision origin_url current_revision fetched_revision
    expected_revision="$(configuration_source_revision)"

    if [[ ! -d "$REPO_DIR/.git" ]]; then
        rm -rf "$REPO_DIR"
        canonical_git clone --depth 1 --branch "$PRIVATE_BRANCH" "$PRIVATE_REPO" "$REPO_DIR"
        harden_canonical_repo_permissions
        assert_canonical_repo_trust
        current_revision="$(canonical_git -C "$REPO_DIR" rev-parse HEAD)"
        [[ "$current_revision" == "$expected_revision" ]] \
            || die "Во время первого canonical clone ветка ${PRIVATE_BRANCH} изменилась: PVE Configuration запущена из ${expected_revision}, а clone получил ${current_revision}. Ничего из новой revision не применяется; повторите Public Bootstrap."
        assert_clean_git_worktree "$REPO_DIR" "Canonical"
    else
        # Миграция старой модели pvedeploy-owned checkout выполняется до любых
        # Git-команд от root. Содержимое не очищается: local drift всё равно STOP.
        harden_canonical_repo_permissions
        assert_canonical_repo_trust

        origin_url="$(canonical_git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
        [[ "$origin_url" == "$PRIVATE_REPO" ]] \
            || die "Существующий checkout ${REPO_DIR} имеет неожиданный origin '${origin_url:-не задан}'. Ожидается '${PRIVATE_REPO}'. Автоматическая подмена origin запрещена."
        ok "Origin существующего private checkout соответствует каноническому репозиторию"

        assert_clean_git_worktree "$REPO_DIR" "Canonical"
        current_revision="$(canonical_git -C "$REPO_DIR" rev-parse HEAD)"
        if [[ "$current_revision" != "$expected_revision" ]]; then
            canonical_git -C "$REPO_DIR" fetch --depth 1 origin "$PRIVATE_BRANCH"
            fetched_revision="$(canonical_git -C "$REPO_DIR" rev-parse FETCH_HEAD)"
            [[ "$fetched_revision" == "$expected_revision" ]] \
                || die "Ветка ${PRIVATE_BRANCH} изменилась во время PVE Configuration: ожидается ${expected_revision}, fetch получил ${fetched_revision}. Canonical checkout не переключён; повторите Public Bootstrap."
            canonical_git -C "$REPO_DIR" reset --hard "$expected_revision"
            canonical_git -C "$REPO_DIR" clean -ffd
            harden_canonical_repo_permissions
            assert_canonical_repo_trust
            assert_clean_git_worktree "$REPO_DIR" "Canonical"
        else
            ok "Canonical checkout уже находится на revision текущей PVE Configuration и не имеет локального drift"
        fi
    fi

    REPO_REVISION="$(canonical_git -C "$REPO_DIR" rev-parse HEAD)"
    [[ "$REPO_REVISION" == "$expected_revision" ]] \
        || die "Canonical checkout ${REPO_DIR} имеет revision ${REPO_REVISION}, ожидается ${expected_revision}"

    printf '%s\n' "$REPO_REVISION" >"$STATE_DIR/last-revision"
    chown root:"$DEPLOY_USER" "$STATE_DIR/last-revision"
    chmod 0640 "$STATE_DIR/last-revision"
    ok "Canonical private checkout root-trusted и зафиксирован на revision: ${REPO_REVISION}"
}
