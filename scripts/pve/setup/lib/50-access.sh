#!/usr/bin/env bash

ensure_managed_pool() {
    log "Проверка пула ресурсов ${MANAGED_POOL}"
    if pveum pool list --output-format json 2>/dev/null \
        | jq -e --arg p "$MANAGED_POOL" '.[] | select(.poolid == $p)' >/dev/null; then
        ok "Пул ресурсов ${MANAGED_POOL} уже существует"
    else
        pveum pool add "$MANAGED_POOL" --comment "Рабочая зона автоматизации проекта Proxmox"
        ok "Создан пул ресурсов ${MANAGED_POOL}"
    fi
}

priv_lines() {
    printf '%s\n' "$1" | tr ', ' '\n\n' | sed '/^$/d' | sort -u
}

join_lines() {
    if [[ -z "${1:-}" ]]; then printf 'нет'; else printf '%s\n' "$1" | paste -sd, -; fi
}

ensure_role() {
    local role=$1 expected_raw=$2
    local role_json actual_raw expected actual missing extra missing_raw missing_after dangerous_extra

    role_json="$(pveum role list --output-format json)"
    if ! jq -e --arg role "$role" '.[] | select(.roleid == $role)' <<<"$role_json" >/dev/null; then
        pveum role add "$role" --privs "$expected_raw"
        ok "Создана роль ${role}"
        return
    fi

    actual_raw="$(jq -r --arg role "$role" '.[] | select(.roleid == $role) | (.privs // "")' \
        <<<"$role_json" | head -n1)"
    expected="$(priv_lines "$expected_raw")"
    actual="$(priv_lines "$actual_raw")"
    missing="$(comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true)"
    extra="$(comm -13 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true)"

    if [[ -n "$missing" ]]; then
        missing_raw="$(printf '%s\n' "$missing" | paste -sd' ' -)"
        pveum role modify "$role" --append 1 --privs "$missing_raw"
        ok "Роль ${role} дополнена недостающими privileges: $(join_lines "$missing")"

        role_json="$(pveum role list --output-format json)"
        actual_raw="$(jq -r --arg role "$role" '.[] | select(.roleid == $role) | (.privs // "")' \
            <<<"$role_json" | head -n1)"
        actual="$(priv_lines "$actual_raw")"
        missing_after="$(comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true)"
        [[ -z "$missing_after" ]] \
            || die "После дополнения роли ${role} по-прежнему отсутствуют privileges: $(join_lines "$missing_after")"
    else
        ok "Роль ${role} уже содержит все требуемые privileges"
    fi

    if [[ -n "$extra" ]]; then
        dangerous_extra="$(printf '%s\n' "$extra" | grep -E '^(Permissions\.Modify|Sys\.Modify)$' || true)"
        if [[ -n "$dangerous_extra" ]]; then
            warn "Роль ${role} содержит опасные дополнительные privileges, которые PVE Configuration по принятой additive policy не удаляет автоматически: $(join_lines "$dangerous_extra")"
        fi
        info "Роль ${role} содержит дополнительные privileges, они сохранены без изменений: $(join_lines "$extra")"
    fi
}

ensure_roles() {
    log "Проверка ролей Proxmox"
    ensure_role "$ROLE_CLONE" "$ROLE_CLONE_PRIVS"
    ensure_role "$ROLE_GUEST" "$ROLE_GUEST_PRIVS"
    ensure_role "$ROLE_NETWORK" "$ROLE_NETWORK_PRIVS"
    ensure_role "$ROLE_STORAGE" "$ROLE_STORAGE_PRIVS"
    ensure_role "$ROLE_POOL" "$ROLE_POOL_PRIVS"
}

ensure_pve_user() {
    local userid=$1 comment=$2 users_json enabled
    users_json="$(pveum user list --output-format json)"

    if jq -e --arg id "$userid" '.[] | select(.userid == $id)' <<<"$users_json" >/dev/null; then
        enabled="$(jq -r --arg id "$userid" '.[] | select(.userid == $id) | (.enable // 1)' \
            <<<"$users_json" | head -n1)"
        [[ "$enabled" != "0" ]] \
            || die "Пользователь PVE ${userid} существует, но отключён. PVE Configuration не включает существующую учётную запись автоматически, чтобы не отменить осознанную блокировку. Сначала проверьте причину и включите пользователя вручную, если это действительно требуется."
        ok "Пользователь PVE ${userid} уже существует и включён"
    else
        pveum user add "$userid" --comment "$comment" --enable 1
        ok "Создан пользователь PVE ${userid}"
    fi
}

token_entry() {
    local userid=$1 token_name=$2
    pveum user token list "$userid" --output-format json 2>/dev/null \
        | jq -c --arg id "$token_name" '.[] | select(.tokenid == $id)' | head -n1
}

write_token_secret() {
    local file=$1 full_token=$2 secret=$3 group=$4 mode=$5 old_umask
    old_umask="$(umask)"
    umask 077
    printf 'token_id=%s\ntoken_secret=%s\n' "$full_token" "$secret" >"$file"
    umask "$old_umask"
    chown root:"$group" "$file"
    chmod "$mode" "$file"
}

ensure_token() {
    local userid=$1 token_name=$2 full_token=$3 secret_file=$4
    local file_group=$5 file_mode=$6 comment=$7
    local entry privsep out secret returned_id

    entry="$(token_entry "$userid" "$token_name" || true)"
    if [[ -n "$entry" ]]; then
        privsep="$(jq -r '.privsep // 1' <<<"$entry")"
        if [[ "$privsep" != "1" ]]; then
            warn "API-токен ${full_token} уже существует с privsep=${privsep}; настройка не изменяется автоматически, чтобы не ограничить уже работающий доступ"
            printf '       При privsep=0 токен использует права базового пользователя %s.\n' "$userid"
            printf '       Отдельные ACL токена в этом режиме не служат дополнительным ограничителем.\n'
            printf '       Если перевод на privsep=1 понадобится, это должно быть отдельным осознанным действием после проверки текущих потребителей токена.\n'
        else
            ok "API-токен ${full_token} использует privsep=1"
        fi

        [[ -f "$secret_file" ]] \
            || die "API-токен ${full_token} существует, но локальный файл секрета ${secret_file} отсутствует. Требуется явная ротация или восстановление учётных данных."

        chown root:"$file_group" "$secret_file"
        chmod "$file_mode" "$secret_file"
        ok "API-токен ${full_token} уже существует, локальный секрет найден"
        return
    fi

    [[ ! -f "$secret_file" ]] \
        || warn "Файл ${secret_file} существует, но API-токен ${full_token} отсутствует; будет создан новый токен и локальное значение будет заменено"

    out="$(pveum user token add "$userid" "$token_name" --privsep 1 --comment "$comment" --output-format json)"
    secret="$(jq -r '.value // .data.value // empty' <<<"$out")"
    returned_id="$(jq -r '."full-tokenid" // .data."full-tokenid" // empty' <<<"$out")"

    [[ -n "$secret" ]] || die "Proxmox создал ${full_token}, но одноразовый секрет не удалось разобрать"
    [[ -z "$returned_id" || "$returned_id" == "$full_token" ]] \
        || die "Proxmox вернул неожиданный идентификатор API-токена: ${returned_id}"

    write_token_secret "$secret_file" "$full_token" "$secret" "$file_group" "$file_mode"
    unset secret out
    ok "Создан API-токен ${full_token} с privsep=1, секрет сохранён на PVE"
}

acl_entry() {
    local path=$1 type=$2 ugid=$3 role=$4
    pveum acl list --output-format json \
        | jq -c --arg path "$path" --arg type "$type" --arg ugid "$ugid" --arg role "$role" \
            '.[] | select(.path == $path and .type == $type and .ugid == $ugid and .roleid == $role)' \
        | head -n1
}

ensure_acl_entry() {
    local path=$1 type=$2 ugid=$3 role=$4
    local entry propagate option label

    entry="$(acl_entry "$path" "$type" "$ugid" "$role" || true)"
    if [[ -n "$entry" ]]; then
        propagate="$(jq -r '.propagate // 1' <<<"$entry")"
        if [[ "$propagate" == "1" ]]; then
            ok "ACL уже существует: ${path}, ${type}=${ugid}, роль=${role}"
        else
            warn "ACL ${path}, ${type}=${ugid}, роль=${role} существует с propagate=${propagate}; скрипт его не изменяет"
        fi
        return
    fi

    case "$type" in
        user) option="--users"; label="пользователь" ;;
        token) option="--tokens"; label="API-токен" ;;
        *) die "Неподдерживаемый тип субъекта ACL: $type" ;;
    esac

    pveum acl modify "$path" "$option" "$ugid" --roles "$role" --propagate 1
    ok "Добавлен ACL: ${path}, ${label}=${ugid}, роль=${role}"
}

ensure_host_identity_acls() {
    local userid=$1 tokenid=$2 principal_type principal

    for principal_type in user token; do
        if [[ "$principal_type" == "user" ]]; then principal="$userid"; else principal="$tokenid"; fi

        ensure_acl_entry "/vms" "$principal_type" "$principal" "$ROLE_GUEST"
        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_POOL"
        ensure_acl_entry "/storage/local" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/storage/local-lvm" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/sdn/zones/localnetwork/${BRIDGE}" "$principal_type" "$principal" "$ROLE_NETWORK"
    done
}

ensure_ai_identity_acls() {
    local userid=$1 tokenid=$2 principal_type principal

    for principal_type in user token; do
        if [[ "$principal_type" == "user" ]]; then principal="$userid"; else principal="$tokenid"; fi

        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_GUEST"
        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_POOL"
        ensure_acl_entry "/vms/${TEMPLATE_VMID}" "$principal_type" "$principal" "$ROLE_CLONE"
        ensure_acl_entry "/storage/local" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/storage/local-lvm" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/sdn/zones/localnetwork/${BRIDGE}" "$principal_type" "$principal" "$ROLE_NETWORK"
    done
}

verify_permission_set() {
    local full_token=$1 permissions_json=$2 path=$3 required_raw=$4
    local priv missing=""

    while IFS= read -r priv; do
        [[ -n "$priv" ]] || continue
        if ! jq -e --arg path "$path" --arg priv "$priv" \
            '((.[$path] // {}) | has($priv))' <<<"$permissions_json" >/dev/null; then
            missing+="${priv}"$'\n'
        fi
    done < <(priv_lines "$required_raw")

    [[ -z "$missing" ]] \
        || die "API-токен ${full_token} не имеет ожидаемых effective permissions на ${path}: $(join_lines "${missing%$'\n'}")"
}

verify_effective_permissions() {
    local userid=$1 token_name=$2 full_token=$3 scope=$4 permissions_json

    permissions_json="$(pveum user token permissions "$userid" "$token_name" --output-format json)" \
        || die "Не удалось получить effective permissions для API-токена ${full_token}"

    case "$scope" in
        host)
            verify_permission_set "$full_token" "$permissions_json" "/vms" "$ROLE_GUEST_PRIVS"
            verify_permission_set "$full_token" "$permissions_json" "/pool/${MANAGED_POOL}" "$ROLE_POOL_PRIVS"
            ;;
        ai)
            verify_permission_set "$full_token" "$permissions_json" "/pool/${MANAGED_POOL}" \
                "${ROLE_GUEST_PRIVS} ${ROLE_POOL_PRIVS}"
            verify_permission_set "$full_token" "$permissions_json" "/vms/${TEMPLATE_VMID}" "$ROLE_CLONE_PRIVS"
            ;;
        *) die "Неизвестная модель effective permissions '${scope}' для ${full_token}" ;;
    esac

    verify_permission_set "$full_token" "$permissions_json" "/storage/local" "$ROLE_STORAGE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/storage/local-lvm" "$ROLE_STORAGE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/sdn/zones/localnetwork/${BRIDGE}" "$ROLE_NETWORK_PRIVS"

    ok "Effective permissions API-токена ${full_token} соответствуют модели ${scope}"
}

verify_token_api_auth() {
    local full_token=$1 secret_file=$2
    local stored_id secret old_umask

    stored_id="$(sed -n 's/^token_id=//p' "$secret_file" | head -n1)"
    secret="$(sed -n 's/^token_secret=//p' "$secret_file" | head -n1)"

    [[ "$stored_id" == "$full_token" ]] \
        || die "В ${secret_file} записан token_id '${stored_id:-не задан}', ожидается '${full_token}'"
    [[ -n "$secret" ]] || die "В ${secret_file} отсутствует token_secret"

    find "$SECRETS_DIR" -maxdepth 1 -type f -name '.api-check.*' -delete \
        || die "Не удалось удалить оставшиеся временные файлы проверки API credential"

    old_umask="$(umask)"
    umask 077
    API_HEADER_FILE="$(mktemp "${SECRETS_DIR}/.api-check.XXXXXX")"
    umask "$old_umask"
    printf 'Authorization: PVEAPIToken=%s=%s\n' "$full_token" "$secret" >"$API_HEADER_FILE"
    chmod 0600 "$API_HEADER_FILE"

    if ! curl --fail --silent --show-error --insecure \
        --connect-timeout 10 --max-time 20 \
        --header "@${API_HEADER_FILE}" \
        https://127.0.0.1:8006/api2/json/version \
        | jq -e '(.data.version // .data.release // empty) != ""' >/dev/null; then
        cleanup_api_header_file
        unset secret
        die "API-токен ${full_token} не прошёл локальную проверку авторизации. Возможен устаревший secret или другая проблема credential."
    fi

    cleanup_api_header_file
    unset secret
    ok "API-токен ${full_token} успешно авторизуется в локальном Proxmox API"
}

verify_pve_identity() {
    local userid=$1 token_name=$2 full_token=$3 secret_file=$4 scope=$5
    verify_effective_permissions "$userid" "$token_name" "$full_token" "$scope"
    verify_token_api_auth "$full_token" "$secret_file"
}

ensure_pve_identities() {
    log "Проверка служебных учётных записей, API-токенов и ACL Proxmox"

    ensure_pve_user "$HOST_PVE_USER" \
        "Учётная запись человека и локальных инструментов PVE для прямого развёртывания"
    ensure_pve_user "$AI_PVE_USER" \
        "Учётная запись контура AI, используемая Proximo"

    ensure_token "$HOST_PVE_USER" "$HOST_PVE_TOKEN_NAME" "$HOST_PVE_TOKEN" \
        "$HOST_TOKEN_FILE" "$DEPLOY_USER" 0640 \
        "API-токен для deploy-guest и прямой локальной автоматизации PVE"
    ensure_token "$AI_PVE_USER" "$AI_PVE_TOKEN_NAME" "$AI_PVE_TOKEN" \
        "$AI_TOKEN_FILE" root 0600 \
        "Инфраструктурный API-токен для контура AI / Proximo"

    ensure_host_identity_acls "$HOST_PVE_USER" "$HOST_PVE_TOKEN"
    ensure_ai_identity_acls "$AI_PVE_USER" "$AI_PVE_TOKEN"

    verify_pve_identity "$HOST_PVE_USER" "$HOST_PVE_TOKEN_NAME" "$HOST_PVE_TOKEN" "$HOST_TOKEN_FILE" host
    verify_pve_identity "$AI_PVE_USER" "$AI_PVE_TOKEN_NAME" "$AI_PVE_TOKEN" "$AI_TOKEN_FILE" ai
}
