#!/usr/bin/env bash

# cloud-init can exit with rc=2 after a successful run when only recoverable
# warnings are present. Current Proxmox-generated Cloud-Init user-data may
# trigger exactly this with the deprecated scalar 'user' field. For status
# inspection we normalize the guest command to exit 0 and decide from stdout.
cloud_init_status_output() {
    local vmid=$1
    smoke_guest_exec "$vmid" /bin/bash -lc 'cloud-init status --long 2>&1 || true'
}

cloud_init_status_done() {
    grep -qE '^status:[[:space:]]+done$'
}

cloud_init_status_error() {
    grep -qE '^status:[[:space:]]+error$'
}

cloud_init_status_degraded() {
    grep -qE '^extended_status:[[:space:]]+degraded done$'
}

# Replace the builder wait with cloud-init-aware exit-code normalization.
template_wait_for_cloud_init() {
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local out

    while (( SECONDS < deadline )); do
        out="$(cloud_init_status_output "$TEMPLATE_VMID" 2>/dev/null || true)"
        if cloud_init_status_done <<<"$out"; then
            if cloud_init_status_degraded <<<"$out"; then
                info "Cloud-Init завершён со статусом degraded done; recoverable warnings не блокируют template pipeline"
            fi
            ok "Cloud-Init завершён через $((SECONDS - started)) с"
            return 0
        fi
        if cloud_init_status_error <<<"$out"; then
            printf '%s\n' "$out" >&2
            return 1
        fi
        if (( SECONDS >= next_report )); then
            template_wait_progress "Cloud-Init" "$started" "$TEMPLATE_WAIT_SECONDS" ', QGA=ok; status ещё не done'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

# Replace the Full Clone wait for the same rc=2 semantics.
smoke_wait_for_cloud_init() {
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local out

    while (( SECONDS < deadline )); do
        out="$(cloud_init_status_output "$SMOKE_VMID" 2>/dev/null || true)"
        if cloud_init_status_done <<<"$out"; then
            if cloud_init_status_degraded <<<"$out"; then
                info "Smoke VM Cloud-Init завершён со статусом degraded done; recoverable warnings приняты"
            fi
            ok "Smoke VM Cloud-Init завершён через $((SECONDS - started)) с"
            return 0
        fi
        if cloud_init_status_error <<<"$out"; then
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

# Replace smoke guest verification so pipefail does not convert cloud-init's
# successful degraded rc=2 into a false guest-verification failure.
smoke_verify_guest() {
    smoke_guest_exec "$SMOKE_VMID" /bin/bash -lc '
set -Eeuo pipefail
min_root_bytes=$1
kernel="$(uname -r)"
[[ "$kernel" == *-amd64 ]]
[[ "$kernel" != *cloud* ]]
systemctl is-active --quiet qemu-guest-agent.service
systemctl is-active --quiet ssh.service
cloud_init_status="$(cloud-init status --long 2>&1 || true)"
grep -qE "^status:[[:space:]]+done$" <<<"$cloud_init_status"
! grep -qE "^status:[[:space:]]+error$" <<<"$cloud_init_status"
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
