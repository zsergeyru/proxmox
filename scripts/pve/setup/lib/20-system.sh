#!/usr/bin/env bash

configure_ceph_repository() {
    local ceph_sources="/etc/apt/sources.list.d/ceph.sources"
    local legacy_ceph_list="/etc/apt/sources.list.d/ceph.list"
    local uri suite component uri_count suite_count component_count release tmp

    if [[ -f "$legacy_ceph_list" ]] \
        && grep -Ev '^[[:space:]]*(#|$)' "$legacy_ceph_list" | grep -qi 'ceph'; then
        die "Обнаружен активный legacy Ceph repository ${legacy_ceph_list}. Для PVE 9 ожидается ceph.sources; автоматическое изменение нестандартной конфигурации запрещено."
    fi

    if [[ ! -f "$ceph_sources" ]]; then
        ok "Ceph repository не настроен; изменений не требуется"
        return
    fi

    uri_count="$(grep -Ec '^URIs:[[:space:]]*' "$ceph_sources" || true)"
    suite_count="$(grep -Ec '^Suites:[[:space:]]*' "$ceph_sources" || true)"
    component_count="$(grep -Ec '^Components:[[:space:]]*' "$ceph_sources" || true)"

    [[ "$uri_count" == "1" && "$suite_count" == "1" && "$component_count" == "1" ]] \
        || die "Файл ${ceph_sources} содержит несколько или неполные repository stanzas. PVE Configuration не будет переписывать нестандартную Ceph-конфигурацию автоматически."

    uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$ceph_sources" | head -n1)"
    suite="$(sed -n 's/^Suites:[[:space:]]*//p' "$ceph_sources" | head -n1)"
    component="$(sed -n 's/^Components:[[:space:]]*//p' "$ceph_sources" | head -n1)"

    if [[ "$uri" =~ ^https?://download\.proxmox\.com/debian/(ceph-[A-Za-z0-9._-]+)$ \
        && "$suite" == "trixie" && "$component" == "no-subscription" ]]; then
        ok "Ceph repository уже использует no-subscription (${BASH_REMATCH[1]})"
        return
    fi

    if [[ "$uri" =~ ^https?://enterprise\.proxmox\.com/debian/(ceph-[A-Za-z0-9._-]+)$ \
        && "$suite" == "trixie" && "$component" == "enterprise" ]]; then
        release="${BASH_REMATCH[1]}"
        tmp="$(mktemp "${ceph_sources}.tmp.XXXXXX")"

        if ! sed \
            -e "s#^URIs:[[:space:]]*https\?://enterprise\.proxmox\.com/debian/${release}[[:space:]]*\$#URIs: http://download.proxmox.com/debian/${release}#" \
            -e 's/^Components:[[:space:]]*enterprise[[:space:]]*$/Components: no-subscription/' \
            "$ceph_sources" >"$tmp"; then
            rm -f -- "$tmp"
            die "Не удалось преобразовать Ceph enterprise repository в no-subscription"
        fi

        if ! install -o root -g root -m 0644 "$tmp" "$ceph_sources"; then
            rm -f -- "$tmp"
            die "Не удалось установить обновлённую конфигурацию Ceph repository"
        fi
        rm -f -- "$tmp"

        uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$ceph_sources" | head -n1)"
        component="$(sed -n 's/^Components:[[:space:]]*//p' "$ceph_sources" | head -n1)"
        [[ "$uri" == "http://download.proxmox.com/debian/${release}" \
            && "$component" == "no-subscription" ]] \
            || die "После изменения не удалось подтвердить Ceph no-subscription repository для ${release}"

        ok "Ceph ${release}: enterprise repository переведён на no-subscription без смены release"
        return
    fi

    die "Файл ${ceph_sources} имеет нестандартную Ceph repository configuration (URIs='${uri:-не задан}', Suites='${suite:-не задан}', Components='${component:-не задан}'). PVE Configuration не будет переписывать её автоматически."
}

configure_pve_repository() {
    local pve_sources="/etc/apt/sources.list.d/proxmox.sources"
    local old_bootstrap_sources="/etc/apt/sources.list.d/pve-no-subscription.sources"
    local uri suite component uri_count suite_count component_count

    if [[ -f "$pve_sources" ]]; then
        uri_count="$(grep -Ec '^URIs:[[:space:]]*' "$pve_sources" || true)"
        suite_count="$(grep -Ec '^Suites:[[:space:]]*' "$pve_sources" || true)"
        component_count="$(grep -Ec '^Components:[[:space:]]*' "$pve_sources" || true)"
        [[ "$uri_count" == "1" && "$suite_count" == "1" && "$component_count" == "1" ]] \
            || die "Файл ${pve_sources} содержит несколько или неполные repository stanzas. PVE Configuration не будет переписывать нестандартную PVE-конфигурацию автоматически."

        uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$pve_sources" | head -n1)"
        suite="$(sed -n 's/^Suites:[[:space:]]*//p' "$pve_sources" | head -n1)"
        component="$(sed -n 's/^Components:[[:space:]]*//p' "$pve_sources" | head -n1)"
        [[ "$uri" == "http://download.proxmox.com/debian/pve" \
            && "$suite" == "trixie" && "$component" == "pve-no-subscription" ]] \
            || die "Существующий ${pve_sources} имеет неожиданную конфигурацию; автоматическая перезапись запрещена"
        ok "PVE repository уже использует pve-no-subscription"
    else
        cat >"$pve_sources" <<'EOF_PVE_REPO'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF_PVE_REPO
        chmod 0644 "$pve_sources"
        ok "Создан PVE no-subscription repository: ${pve_sources}"
    fi

    if [[ -f "$old_bootstrap_sources" ]]; then
        uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$old_bootstrap_sources" | head -n1)"
        suite="$(sed -n 's/^Suites:[[:space:]]*//p' "$old_bootstrap_sources" | head -n1)"
        component="$(sed -n 's/^Components:[[:space:]]*//p' "$old_bootstrap_sources" | head -n1)"
        if [[ "$uri" == "http://download.proxmox.com/debian/pve" \
            && "$suite" == "trixie" && "$component" == "pve-no-subscription" ]]; then
            rm -f -- "$old_bootstrap_sources"
            ok "Удалён дублирующий старый bootstrap repository: ${old_bootstrap_sources}"
        else
            die "${old_bootstrap_sources} существует, но имеет неожиданное содержимое; автоматическое удаление запрещено"
        fi
    fi
}

disable_enterprise_repository_file() {
    local active=$1 disabled="${1}.disabled"
    [[ -f "$active" ]] || return 0

    if [[ -e "$disabled" ]]; then
        if cmp -s "$active" "$disabled"; then
            rm -f -- "$active"
            ok "Активный enterprise repository совпадает с уже сохранённой disabled-копией и удалён: ${active}"
            return
        fi
        die "Нельзя отключить ${active}: ${disabled} уже существует с другим содержимым. Автоматическая перезапись backup запрещена."
    fi

    mv -- "$active" "$disabled"
    ok "Отключён репозиторий enterprise: ${active} -> ${disabled}"
}

configure_apt() {
    log "Настройка PVE/Ceph repository policy без subscription"

    install -d -m 0755 /etc/apt/sources.list.d
    configure_pve_repository

    local f
    for f in \
        /etc/apt/sources.list.d/pve-enterprise.list \
        /etc/apt/sources.list.d/pve-enterprise.sources; do
        disable_enterprise_repository_file "$f"
    done

    configure_ceph_repository

    apt-get update
    if (( UPDATE_SYSTEM )); then
        log "Выполняется явно запрошенное полное обновление Proxmox/Debian"
        DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
    fi
}

install_packages() {
    log "Установка обязательных пакетов конфигурации и администрирования"

    local packages=(
        git openssh-client python3 python3-yaml python3-jsonschema curl jq ca-certificates
        mc htop tmux smartmontools lm-sensors
    )

    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"

    for cmd in git ssh ssh-keygen python3 curl jq runuser find; do
        require_cmd "$cmd"
    done

    python3 - <<'PY_DEPS' \
        || die "Python-зависимости PVE Configuration установлены не полностью"
import yaml
from jsonschema import Draft202012Validator

assert yaml is not None
assert Draft202012Validator is not None
PY_DEPS

    ok "Обязательные пакеты установлены"
}

check_time_dns_network() {
    log "Проверка времени, DNS и исходящего подключения"

    date --iso-8601=seconds

    if command -v timedatectl >/dev/null 2>&1; then
        timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes \
            || warn "Синхронизация времени NTP пока не подтверждена"
    fi

    getent ahosts github.com >/dev/null || die "Не работает DNS-разрешение имени github.com"
    getent ahosts download.proxmox.com >/dev/null || die "Не работает DNS-разрешение имени download.proxmox.com"

    curl -fsS --connect-timeout 10 --max-time 20 -o /dev/null https://github.com/ \
        || die "Нет HTTPS-доступа к GitHub"
    curl -fsS --connect-timeout 10 --max-time 20 -o /dev/null \
        http://download.proxmox.com/debian/pve/dists/trixie/InRelease \
        || die "Нет доступа к PVE repository download.proxmox.com"

    ok "DNS, GitHub HTTPS и доступ к PVE repository работают"
}
