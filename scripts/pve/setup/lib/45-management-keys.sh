#!/usr/bin/env bash

PUBLIC_KEYS_DIR="${RUNTIME_DIR}/public-keys"
MANAGEMENT_KEYS_AGGREGATE="${PUBLIC_KEYS_DIR}/management-authorized-keys"
DEPLOYER_REGISTRY_KEY="${PUBLIC_KEYS_DIR}/deployer.pub"

management_key_fingerprint() {
    local file=$1 fingerprint
    fingerprint="$(ssh-keygen -lf "$file" -E sha256 2>/dev/null | awk 'NR == 1 {print $2}')" \
        || return 1
    [[ "$fingerprint" == SHA256:* ]] || return 1
    printf '%s\n' "$fingerprint"
}

validate_management_public_key_file() {
    local file=$1 label=$2 line_count key_type key_data fingerprint

    [[ ! -L "$file" ]] || die "${label} является симлинком; автоматическая работа с registry запрещена"
    [[ -f "$file" ]] || die "${label} должен быть обычным файлом"

    line_count="$(awk 'NF {count++} END {print count+0}' "$file")"
    [[ "$line_count" == "1" ]] \
        || die "${label} должен содержать ровно один непустой OpenSSH public key"

    read -r key_type key_data _ < <(awk 'NF {print; exit}' "$file")
    [[ "$key_type" == "ssh-ed25519" && -n "$key_data" ]] \
        || die "${label} должен содержать Ed25519 public key без authorized_keys options"

    fingerprint="$(management_key_fingerprint "$file")" \
        || die "Не удалось вычислить fingerprint для ${label}"
    [[ -n "$fingerprint" ]] || die "Пустой fingerprint для ${label}"
}

write_registry_public_key_atomic() {
    local source=$1 target=$2 tmp
    tmp="$(mktemp "${PUBLIC_KEYS_DIR}/.pve-config-key.XXXXXX")"
    awk 'NF {print; exit}' "$source" >"$tmp"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$tmp"
    chmod 0644 "$tmp"
    mv -f -- "$tmp" "$target"
}

rebuild_management_authorized_keys() {
    local tmp base file
    tmp="$(mktemp "${PUBLIC_KEYS_DIR}/.pve-config-aggregate.XXXXXX")"

    awk 'NF {print; exit}' "$DEPLOYER_REGISTRY_KEY" >"$tmp"
    while IFS= read -r base; do
        [[ -n "$base" ]] || continue
        file="${PUBLIC_KEYS_DIR}/${base}"
        awk 'NF {print; exit}' "$file" >>"$tmp"
    done < <(
        find "$PUBLIC_KEYS_DIR" -mindepth 1 -maxdepth 1 -type f \
            -regextype posix-extended -regex '.*/[1-9][0-9]{2}\.pub' \
            -printf '%f\n' | LC_ALL=C sort
    )

    chown "$DEPLOY_USER:$DEPLOY_USER" "$tmp"
    chmod 0644 "$tmp"

    if [[ -f "$MANAGEMENT_KEYS_AGGREGATE" && ! -L "$MANAGEMENT_KEYS_AGGREGATE" ]] \
        && cmp -s "$tmp" "$MANAGEMENT_KEYS_AGGREGATE"; then
        rm -f -- "$tmp"
        chown "$DEPLOY_USER:$DEPLOY_USER" "$MANAGEMENT_KEYS_AGGREGATE"
        chmod 0644 "$MANAGEMENT_KEYS_AGGREGATE"
        ok "management-authorized-keys уже соответствует registry"
        return
    fi

    [[ ! -L "$MANAGEMENT_KEYS_AGGREGATE" ]] \
        || die "${MANAGEMENT_KEYS_AGGREGATE} является симлинком; автоматическая замена запрещена"
    if [[ -e "$MANAGEMENT_KEYS_AGGREGATE" && ! -f "$MANAGEMENT_KEYS_AGGREGATE" ]]; then
        die "${MANAGEMENT_KEYS_AGGREGATE} существует, но не является обычным файлом"
    fi

    mv -f -- "$tmp" "$MANAGEMENT_KEYS_AGGREGATE"
    ok "management-authorized-keys детерминированно пересобран"
}

validate_management_registry_entries() {
    local name file fingerprint
    declare -A seen_fingerprints=()

    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        file="${PUBLIC_KEYS_DIR}/${name}"

        case "$name" in
            management-authorized-keys)
                [[ ! -L "$file" ]] \
                    || die "${file} является симлинком; автоматическая работа запрещена"
                [[ -f "$file" ]] \
                    || die "${file} должен быть обычным файлом"
                continue
                ;;
            deployer.pub)
                ;;
            *.pub)
                [[ "$name" =~ ^[1-9][0-9]{2}\.pub$ ]] \
                    || die "Недопустимое имя management public key в registry: ${name}; ожидается <VMID>.pub для VMID 100-999"
                ;;
            *)
                die "Неизвестный файл в management public-key registry: ${name}"
                ;;
        esac

        validate_management_public_key_file "$file" "$file"
        fingerprint="$(management_key_fingerprint "$file")"
        if [[ -n "${seen_fingerprints[$fingerprint]:-}" ]]; then
            die "Один management public key зарегистрирован дважды: ${seen_fingerprints[$fingerprint]} и ${name} (${fingerprint})"
        fi
        seen_fingerprints[$fingerprint]="$name"
        chown "$DEPLOY_USER:$DEPLOY_USER" "$file"
        chmod 0644 "$file"
    done < <(find "$PUBLIC_KEYS_DIR" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)
}

ensure_management_public_key_registry() {
    log "Подготовка канонического management public-key registry"

    [[ ! -L "$PUBLIC_KEYS_DIR" ]] \
        || die "${PUBLIC_KEYS_DIR} является симлинком; автоматическая подготовка registry запрещена"
    if [[ -e "$PUBLIC_KEYS_DIR" && ! -d "$PUBLIC_KEYS_DIR" ]]; then
        die "${PUBLIC_KEYS_DIR} существует, но не является каталогом"
    fi

    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$PUBLIC_KEYS_DIR"

    validate_management_public_key_file "$PVE_GUEST_PUB" "$PVE_GUEST_PUB"

    local canonical_fp registry_fp
    canonical_fp="$(management_key_fingerprint "$PVE_GUEST_PUB")"

    if [[ -e "$DEPLOYER_REGISTRY_KEY" ]]; then
        validate_management_public_key_file "$DEPLOYER_REGISTRY_KEY" "$DEPLOYER_REGISTRY_KEY"
        registry_fp="$(management_key_fingerprint "$DEPLOYER_REGISTRY_KEY")"
        [[ "$registry_fp" == "$canonical_fp" ]] \
            || die "${DEPLOYER_REGISTRY_KEY} (${registry_fp}) не соответствует штатной deployer identity ${PVE_GUEST_PUB} (${canonical_fp}); автоматическая ротация запрещена"
        chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOYER_REGISTRY_KEY"
        chmod 0644 "$DEPLOYER_REGISTRY_KEY"
        ok "deployer.pub уже соответствует штатной deployer identity: ${canonical_fp}"
    else
        write_registry_public_key_atomic "$PVE_GUEST_PUB" "$DEPLOYER_REGISTRY_KEY"
        ok "Зарегистрирован deployer.pub: ${canonical_fp}"
    fi

    validate_management_registry_entries
    rebuild_management_authorized_keys
    validate_management_registry_entries

    chown "$DEPLOY_USER:$DEPLOY_USER" "$PUBLIC_KEYS_DIR"
    chmod 0750 "$PUBLIC_KEYS_DIR"
    ok "Management public-key registry готов: ${PUBLIC_KEYS_DIR}"
}
