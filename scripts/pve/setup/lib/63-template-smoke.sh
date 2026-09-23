#!/usr/bin/env bash

smoke_marker() {
    printf 'smoke-owner=pve-configuration; source-template=%s; template-version=%s' \
        "$TEMPLATE_VMID" "$TEMPLATE_VERSION"
}

smoke_description() {
    printf 'PVE Configuration Full Clone smoke test; %s' "$(smoke_marker)"
}

smoke_guest_exec() {
    local vmid=$1
    shift
    # Повторно используем тот же нормализованный QGA executor, что и template builder.
    (
        TEMPLATE_VMID="$vmid"
        template_guest_exec "$@"
    )
}

template_smoke_state_status() {
    [[ -f "$TEMPLATE_SMOKE_STATE_FILE" ]] || {
        printf '%s\n' 'none'
        return 0
    }

    jq -e \
        --argjson vmid "$TEMPLATE_VMID" \
        --argjson version "$TEMPLATE_VERSION" \
        '.template_vmid == $vmid and .template_version == $version and (.status == "pending" or .status == "passed")' \
        "$TEMPLATE_SMOKE_STATE_FILE" >/dev/null 2>&1 \
        || die "Некорректный или устаревший smoke state ${TEMPLATE_SMOKE_STATE_FILE}; требуется осознанная проверка перед продолжением"

    jq -r '.status' "$TEMPLATE_SMOKE_STATE_FILE"
}

template_smoke_write_state() {
    local status=$1
    local machine_id=${2:-}
    local kernel=${3:-}
    local root_bytes=${4:-0}
    local host_key_fingerprint=${5:-}
    local now tmp

    install -d -o root -g root -m 0750 "$STATE_DIR"
    now="$(date --iso-8601=seconds)"
    tmp="$(mktemp "${STATE_DIR}/.template-smoke.json.tmp.XXXXXX")"

    jq -n \
        --arg status "$status" \
        --arg timestamp "$now" \
        --arg revision "$(state_revision)" \
        --arg machine_id "$machine_id" \
        --arg kernel "$kernel" \
        --arg host_key_fingerprint "$host_key_fingerprint" \
        --argjson template_vmid "$TEMPLATE_VMID" \
        --argjson template_version "$TEMPLATE_VERSION" \
        --argjson smoke_vmid "$SMOKE_VMID" \
        --argjson root_bytes "${root_bytes:-0}" \
        '{
          status: $status,
          timestamp: $timestamp,
          repository_revision: $revision,
          template_vmid: $template_vmid,
          template_version: $template_version,
          smoke_vmid: $smoke_vmid,
          machine_id: $machine_id,
          kernel: $kernel,
          root_filesystem_bytes: $root_bytes,
          ssh_host_key_fingerprint: $host_key_fingerprint
        }' >"$tmp"

    chmod 0644 "$tmp"
    mv -f "$tmp" "$TEMPLATE_SMOKE_STATE_FILE"
}

template_smoke_mark_pending() {
    template_smoke_write_state pending
    info "Template smoke-test помечен pending до успешного Full Clone ${SMOKE_VMID}"
}

template_smoke_record_success() {
    template_smoke_write_state passed "$1" "$2" "$3" "$4"
    ok "Smoke state записан: ${TEMPLATE_SMOKE_STATE_FILE}"
}

smoke_vmid_state() {
    local config

    if pct config "$SMOKE_VMID" >/dev/null 2>&1; then
        printf '%s\n' 'foreign-lxc'
        return 0
    fi

    if ! config="$(qm config "$SMOKE_VMID" 2>/dev/null)"; then
        printf '%s\n' 'free'
        return 0
    fi

    if grep -Fq "$(smoke_marker)" <<<"$config"; then
        printf '%s\n' 'owned-smoke'
    else
        printf '%s\n' 'foreign-vm'
    fi
}

smoke_test_required() {
    local state
    if (( TEMPLATE_BUILT_THIS_RUN || SMOKE_TEST_TEMPLATE )); then
        return 0
    fi

    state="$(template_smoke_state_status)"
    [[ "$state" == 'pending' ]]
}

smoke_wait_progress() {
    local phase=$1 started=$2 limit=$3 detail=${4:-}
    local elapsed vm_state
    elapsed=$((SECONDS - started))
    vm_state="$(qm status "$SMOKE_VMID" 2>/dev/null | awk '{print $2}' || true)"
    [[ -n "$vm_state" ]] || vm_state='unknown'
    wait_msg "${phase}: ${elapsed}s/${limit}s, VM=${vm_state}${detail}"
}

smoke_wait_for_agent() {
    local phase=${1:-QEMU Guest Agent smoke VM}
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS

    while (( SECONDS < deadline )); do
        if qm agent "$SMOKE_VMID" ping >/dev/null 2>&1; then
            ok "${phase} доступен через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            smoke_wait_progress "$phase" "$started" "$TEMPLATE_WAIT_SECONDS" ', agent=нет ответа'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

smoke_wait_for_cloud_init() {
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local out

    while (( SECONDS < deadline )); do
        out="$(smoke_guest_exec "$SMOKE_VMID" /bin/bash -lc 'cloud-init status' 2>/dev/null || true)"
        if grep -q 'status: done' <<<"$out"; then
            ok "Smoke VM Cloud-Init завершён через $((SECONDS - started)) с"
            return 0
        fi
        if grep -q 'status: error' <<<"$out"; then
            printf '%s\n' "$out" >&2
            return 1
        fi
        if (( SECONDS >= next_report )); then
            smoke_wait_progress "Smoke VM Cloud-Init" "$started" "$TEMPLATE_WAIT_SECONDS" ', QGA=ok; status ещё не done'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

smoke_wait_for_ipv4() {
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local ip

    while (( SECONDS < deadline )); do
        ip="$(smoke_guest_exec "$SMOKE_VMID" /bin/bash -lc \
            'for ip in $(hostname -I 2>/dev/null); do [[ "$ip" == *:* ]] || { printf "%s\n" "$ip"; break; }; done' \
            2>/dev/null | head -n1 || true)"
        if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            printf '%s\n' "$ip"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            smoke_wait_progress "Получение IPv4 smoke VM" "$started" "$TEMPLATE_WAIT_SECONDS" ', QGA=ok'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

smoke_ssh() {
    local strict=$1 ip=$2
    shift 2

    ssh \
        -i "$PVE_DEPLOYER_KEY" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=2 \
        -o IdentitiesOnly=yes \
        -o "StrictHostKeyChecking=${strict}" \
        -o "UserKnownHostsFile=${SMOKE_SSH_KNOWN_HOSTS}" \
        -o "HostKeyAlias=${SMOKE_NAME}" \
        -o LogLevel=ERROR \
        "root@${ip}" "$@"
}

smoke_wait_for_ssh() {
    local strict=$1 ip=$2
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local out

    while (( SECONDS < deadline )); do
        if out="$(smoke_ssh "$strict" "$ip" "printf 'SMOKE_SSH_OK\\n'" 2>/dev/null)" \
            && grep -q '^SMOKE_SSH_OK$' <<<"$out"; then
            ok "Root SSH smoke VM подтверждён через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            smoke_wait_progress "Root SSH smoke VM" "$started" "$TEMPLATE_WAIT_SECONDS" ", ip=${ip}"
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

smoke_wait_for_new_boot_id() {
    local previous=$1
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS current

    while (( SECONDS < deadline )); do
        current="$(smoke_guest_exec "$SMOKE_VMID" /bin/cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
        if [[ -n "$current" && "$current" != "$previous" ]]; then
            ok "Smoke VM перезагрузилась через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            smoke_wait_progress "Перезагрузка smoke VM" "$started" "$TEMPLATE_WAIT_SECONDS" ', ожидается новый boot_id/QGA'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

smoke_wait_for_stopped() {
    local limit=300 started=$SECONDS deadline=$((SECONDS + 300)) next_report=$SECONDS

    while (( SECONDS < deadline )); do
        if [[ "$(qm status "$SMOKE_VMID" 2>/dev/null | awk '{print $2}' || true)" == 'stopped' ]]; then
            ok "Smoke VM выключена через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            smoke_wait_progress "Выключение smoke VM" "$started" "$limit"
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 3
    done
    return 1
}

smoke_verify_clone_config() {
    local config=$1 scsi0 disk_size disk_kib required_kib cloudinit_user_data key_body

    grep -q "^name: ${SMOKE_NAME}$" <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} получила неожиданное имя"
    ! grep -q '^template: 1$' <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} неожиданно осталась template"
    grep -Fq "$(smoke_marker)" <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} не содержит safety marker"
    grep -q '^agent: 1$' <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} должна иметь agent=1"
    grep -q '^ciuser: root$' <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} должна иметь ciuser=root"
    grep -q '^ciupgrade: 0$' <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} должна иметь ciupgrade=0"
    grep -q '^ipconfig0: ip=dhcp$' <<<"$config" \
        || die "Smoke VM ${SMOKE_VMID} должна использовать DHCP"

    scsi0="$(sed -n 's/^scsi0: //p' <<<"$config" | head -n1)"
    disk_size="$(tr ',' '\n' <<<"$scsi0" | sed -n 's/^size=//p' | head -n1)"
    disk_kib="$(template_size_to_kib "$disk_size" 2>/dev/null || true)"
    required_kib="$(template_size_to_kib "$SMOKE_DISK_SIZE" 2>/dev/null || true)"
    [[ -n "$disk_kib" && -n "$required_kib" && "$disk_kib" -ge "$required_kib" ]] \
        || die "Smoke VM scsi0 не увеличен до ${SMOKE_DISK_SIZE}; обнаружено ${disk_size:-неизвестно}"

    cloudinit_user_data="$(qm cloudinit dump "$SMOKE_VMID" user)"
    key_body="$(awk 'NF >= 2 {print $2; exit}' "$PVE_DEPLOYER_PUB")"
    [[ -n "$key_body" ]] || die "Не удалось прочитать public key ${PVE_DEPLOYER_PUB}"
    grep -Fq "$key_body" <<<"$cloudinit_user_data" \
        || die "Cloud-Init smoke VM не содержит PVE deployer SSH public key"

    ok "Full Clone config подтверждён: VMID=${SMOKE_VMID}, disk=${disk_size}, DHCP, root SSH key"
}

smoke_verify_guest() {
    smoke_guest_exec "$SMOKE_VMID" /bin/bash -lc '
set -Eeuo pipefail
min_root_bytes=$1
kernel="$(uname -r)"
[[ "$kernel" == *-amd64 ]]
[[ "$kernel" != *cloud* ]]
systemctl is-active --quiet qemu-guest-agent.service
systemctl is-active --quiet ssh.service
cloud-init status | grep -q "status: done"
! id debian >/dev/null 2>&1
passwd -S root | grep -q " L "
sshd_effective="$(/usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1)"
grep -Eq "^permitrootlogin (prohibit-password|without-password)$" <<<"$sshd_effective"
grep -q "^passwordauthentication no$" <<<"$sshd_effective"
grep -q "^kbdinteractiveauthentication no$" <<<"$sshd_effective"
grep -q "^pubkeyauthentication yes$" <<<"$sshd_effective"
[[ -s /root/.ssh/authorized_keys ]]
[[ -s /etc/vm-template-info ]]
machine_id="$(cat /etc/machine-id)"
[[ "$machine_id" =~ ^[0-9a-f]{32}$ ]]
[[ -s /etc/ssh/ssh_host_ed25519_key ]]
[[ -s /etc/ssh/ssh_host_ed25519_key.pub ]]
host_key_fingerprint="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 | awk "{print \$2}")"
[[ -n "$host_key_fingerprint" ]]
root_bytes="$(df -B1 --output=size / | awk "NR==2 {gsub(/[[:space:]]/,\"\",\$1); print \$1}")"
[[ "$root_bytes" =~ ^[0-9]+$ ]]
(( root_bytes >= min_root_bytes ))
printf "SMOKE_MACHINE_ID=%s\n" "$machine_id"
printf "SMOKE_KERNEL=%s\n" "$kernel"
printf "SMOKE_ROOT_BYTES=%s\n" "$root_bytes"
printf "SMOKE_HOST_KEY=%s\n" "$host_key_fingerprint"
printf "SMOKE_GUEST_OK\n"
' bash "$SMOKE_MIN_ROOT_BYTES"
}

run_template_smoke_test() {
    local source_config clone_config verify_before verify_after
    local machine_id_before machine_id_after kernel root_bytes host_key_before host_key_after
    local boot_id_before ip_before ip_after

    log "Full Clone smoke-test template ${TEMPLATE_VMID} через VM ${SMOKE_VMID}"
    require_cmd ssh
    require_cmd jq
    [[ -r "$PVE_DEPLOYER_KEY" && -r "$PVE_DEPLOYER_PUB" ]] \
        || die "Для smoke-test отсутствует PVE deployer SSH identity ${PVE_DEPLOYER_KEY}"

    source_config="$(qm config "$TEMPLATE_VMID")"
    validate_template_contract "$source_config" 1 \
        || die "Перед smoke-test source template ${TEMPLATE_VMID} не прошёл contract check"

    template_smoke_mark_pending

    # С этого момента любой residue VMID 9099 относится к текущей smoke-попытке.
    # Если qm clone упадёт после частичного создания config, пытаемся немедленно
    # пометить эту VM project marker-ом, чтобы следующий run не принял её за foreign.
    SMOKE_ACTIVE=1
    if ! qm clone "$TEMPLATE_VMID" "$SMOKE_VMID" \
        --full 1 \
        --name "$SMOKE_NAME" \
        --storage "$TEMPLATE_DISK_STORAGE"; then
        if qm config "$SMOKE_VMID" >/dev/null 2>&1; then
            qm set "$SMOKE_VMID" --description "$(smoke_description)" >/dev/null 2>&1 || true
        else
            SMOKE_ACTIVE=0
        fi
        die "Не удалось создать Full Clone smoke VM ${SMOKE_VMID} из template ${TEMPLATE_VMID}"
    fi

    # Safety marker записывается первым отдельным изменением сразу после clone.
    qm set "$SMOKE_VMID" --description "$(smoke_description)" \
        || die "Не удалось записать safety marker smoke VM ${SMOKE_VMID}"

    qm set "$SMOKE_VMID" \
        --protection 0 \
        --onboot 0 \
        --ciuser root \
        --ciupgrade 0 \
        --ipconfig0 ip=dhcp \
        --sshkeys "$PVE_DEPLOYER_PUB"
    qm resize "$SMOKE_VMID" scsi0 "$SMOKE_DISK_SIZE"
    qm cloudinit update "$SMOKE_VMID"

    clone_config="$(qm config "$SMOKE_VMID")"
    smoke_verify_clone_config "$clone_config"

    SMOKE_SSH_KNOWN_HOSTS="$(mktemp /run/pve-template-smoke-known-hosts.XXXXXX)"
    chmod 0600 "$SMOKE_SSH_KNOWN_HOSTS"

    log "Первый запуск Full Clone smoke VM ${SMOKE_VMID}"
    qm start "$SMOKE_VMID"
    smoke_wait_for_agent "QEMU Guest Agent smoke VM" \
        || die "QEMU Guest Agent smoke VM не стал доступен за ${TEMPLATE_WAIT_SECONDS} секунд"
    smoke_wait_for_cloud_init \
        || die "Cloud-Init smoke VM не завершился корректно"

    verify_before="$(smoke_verify_guest)" \
        || die "Guest-side проверки Full Clone smoke VM завершились ошибкой"
    grep -q '^SMOKE_GUEST_OK$' <<<"$verify_before" \
        || die "Guest-side smoke verification не вернула SMOKE_GUEST_OK"

    machine_id_before="$(sed -n 's/^SMOKE_MACHINE_ID=//p' <<<"$verify_before" | head -n1)"
    kernel="$(sed -n 's/^SMOKE_KERNEL=//p' <<<"$verify_before" | head -n1)"
    root_bytes="$(sed -n 's/^SMOKE_ROOT_BYTES=//p' <<<"$verify_before" | head -n1)"
    host_key_before="$(sed -n 's/^SMOKE_HOST_KEY=//p' <<<"$verify_before" | head -n1)"
    [[ "$machine_id_before" =~ ^[0-9a-f]{32}$ && "$root_bytes" =~ ^[0-9]+$ && -n "$host_key_before" ]] \
        || die "Не удалось разобрать guest-side smoke verification output"

    ip_before="$(smoke_wait_for_ipv4)" \
        || die "Smoke VM не получила IPv4 за ${TEMPLATE_WAIT_SECONDS} секунд"
    smoke_wait_for_ssh accept-new "$ip_before" \
        || die "Root SSH по PVE deployer key не стал доступен на ${ip_before}"

    boot_id_before="$(smoke_guest_exec "$SMOKE_VMID" /bin/cat /proc/sys/kernel/random/boot_id)"
    [[ -n "$boot_id_before" ]] || die "Не удалось получить boot_id smoke VM перед reboot"

    log "Проверочная перезагрузка Full Clone smoke VM"
    qm reboot "$SMOKE_VMID"
    smoke_wait_for_new_boot_id "$boot_id_before" \
        || die "Smoke VM не завершила проверочную перезагрузку"
    smoke_wait_for_agent "QEMU Guest Agent smoke VM после reboot" \
        || die "QEMU Guest Agent smoke VM не восстановился после reboot"

    verify_after="$(smoke_verify_guest)" \
        || die "Guest-side проверки после reboot завершились ошибкой"
    grep -q '^SMOKE_GUEST_OK$' <<<"$verify_after" \
        || die "Guest-side smoke verification после reboot не вернула SMOKE_GUEST_OK"

    machine_id_after="$(sed -n 's/^SMOKE_MACHINE_ID=//p' <<<"$verify_after" | head -n1)"
    host_key_after="$(sed -n 's/^SMOKE_HOST_KEY=//p' <<<"$verify_after" | head -n1)"
    [[ "$machine_id_after" == "$machine_id_before" ]] \
        || die "machine-id smoke VM изменился после reboot"
    [[ "$host_key_after" == "$host_key_before" ]] \
        || die "SSH host key smoke VM изменился после reboot"

    ip_after="$(smoke_wait_for_ipv4)" \
        || die "Smoke VM не получила IPv4 после reboot"
    smoke_wait_for_ssh yes "$ip_after" \
        || die "Root SSH после reboot не прошёл строгую проверку ранее принятого host key"

    ok "Smoke checks пройдены: kernel=${kernel}, machine-id=${machine_id_before}, rootfs=${root_bytes} bytes, SSH/QGA/Cloud-Init/reboot=ok"

    log "Удаление успешно проверенной smoke VM ${SMOKE_VMID}"
    qm shutdown "$SMOKE_VMID" --timeout 180 \
        || die "Не удалось штатно выключить smoke VM ${SMOKE_VMID}; она оставлена для диагностики"
    smoke_wait_for_stopped \
        || die "Smoke VM ${SMOKE_VMID} не перешла в stopped; она оставлена для диагностики"
    qm destroy "$SMOKE_VMID" --purge 1 \
        || die "Не удалось удалить успешно проверенную smoke VM ${SMOKE_VMID}"

    if qm config "$SMOKE_VMID" >/dev/null 2>&1 || pct config "$SMOKE_VMID" >/dev/null 2>&1; then
        die "После qm destroy VMID ${SMOKE_VMID} всё ещё занят"
    fi

    SMOKE_ACTIVE=0
    cleanup_smoke_known_hosts
    template_smoke_record_success "$machine_id_before" "$kernel" "$root_bytes" "$host_key_before"
    ok "Full Clone smoke-test завершён успешно; временный VMID ${SMOKE_VMID} снова свободен"
}

run_template_smoke_test_if_needed() {
    local existing required=0 state

    existing="$(smoke_vmid_state)"
    if [[ "$existing" == 'owned-smoke' ]]; then
        die "VMID ${SMOKE_VMID} содержит smoke VM от предыдущей неуспешной/прерванной проверки. Она намеренно не удаляется автоматически. Изучите её, затем удалите осознанно и повторите PVE Configuration."
    fi

    if smoke_test_required; then
        required=1
    fi
    (( required )) || return 0

    case "$existing" in
        free) ;;
        foreign-lxc)
            die "Smoke VMID ${SMOKE_VMID} занят LXC-контейнером. Автоматическое изменение/удаление запрещено."
            ;;
        foreign-vm)
            die "Smoke VMID ${SMOKE_VMID} занят обычной VM без project smoke marker. Автоматическое изменение/удаление запрещено."
            ;;
        *)
            die "Не удалось классифицировать состояние smoke VMID ${SMOKE_VMID}: ${existing}"
            ;;
    esac

    state="$(template_smoke_state_status)"
    if (( TEMPLATE_BUILT_THIS_RUN )); then
        info "Smoke-test обязателен: template ${TEMPLATE_VMID} создан текущим run"
    elif (( SMOKE_TEST_TEMPLATE )); then
        info "Smoke-test запрошен явно параметром --smoke-test-template"
    elif [[ "$state" == 'pending' ]]; then
        info "Smoke-test возобновляется по сохранённому pending state"
    fi

    run_template_smoke_test
}
