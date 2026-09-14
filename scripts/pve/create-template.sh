#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical Debian 13 Proxmox template builder.
# Implementation and infrastructure source of truth: https://github.com/zsergeyru/proxmox
# Public zero-day bootstrap repository: https://github.com/zsergeyru/proxmox-bootstrap
#
# The generic Debian cloud image is booted as VMID 9000, configured inside the
# guest through temporary Cloud-Init user-data, rebooted into the regular Debian
# kernel for console/QGA verification, finalized through QEMU Guest Agent, and
# then converted to a protected Proxmox template.
#
# Access model:
#   - ops has no usable password;
#   - root has no usable password;
#   - SSH password authentication is disabled;
#   - SSH public keys are supplied per clone through Proxmox Cloud-Init;
#   - VGA/noVNC tty1 is the primary Proxmox console and autologins ops;
#   - serial0 remains an independent text-console fallback and autologins ops;
#   - Proxmox VM.Console possession is therefore administrative guest access.

VMID="${VMID:-9000}"
TEMPLATE_NAME="${TEMPLATE_NAME:-tpl-debian13}"
BUILDER_NAME="${BUILDER_NAME:-builder-debian13}"
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
SNIPPET_STORAGE="${SNIPPET_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
MEMORY_MB="${MEMORY_MB:-1024}"
CORES="${CORES:-1}"
DISK_SIZE="${DISK_SIZE:-16G}"
WAIT_SECONDS="${WAIT_SECONDS:-1200}"
TEMPLATE_VERSION="${TEMPLATE_VERSION:-4}"

IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
CHECKSUM_URL="${CHECKSUM_URL:-https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS}"
IMAGE_NAME="${IMAGE_URL##*/}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/vz/template/cache/debian13}"
IMAGE_PATH="${IMAGE_DIR}/${IMAGE_NAME}"
CHECKSUM_PATH="${IMAGE_DIR}/SHA512SUMS"
SNIPPET_NAME="debian13-template-builder-${VMID}.yaml"
SNIPPET_VOL="${SNIPPET_STORAGE}:snippets/${SNIPPET_NAME}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

on_error() {
    local rc=$?
    printf '\nBuild stopped with exit code %s.\n' "$rc" >&2
    printf 'The builder VM and its disks are intentionally left in place for inspection.\n' >&2
    printf 'Nothing is destroyed automatically.\n' >&2
    exit "$rc"
}
trap on_error ERR

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

fetch_file() {
    local url=$1
    local dst=$2

    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --retry 3 --output "$dst" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget --tries=3 --output-document="$dst" "$url"
    else
        die "Neither curl nor wget is installed on the Proxmox host"
    fi
}

vm_exists() {
    qm status "$VMID" >/dev/null 2>&1
}

wait_for_agent() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    while (( SECONDS < deadline )); do
        if qm agent "$VMID" ping >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}

wait_for_bootstrap() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    local out
    while (( SECONDS < deadline )); do
        out="$(qm guest exec "$VMID" -- /bin/cat /var/lib/template-build/bootstrap-complete 2>/dev/null || true)"
        if grep -q 'BOOTSTRAP_OK' <<<"$out"; then
            return 0
        fi
        sleep 5
    done
    return 1
}

wait_for_cloud_init() {
    local out
    out="$(qm guest exec "$VMID" -- /bin/bash -lc "timeout ${WAIT_SECONDS}s cloud-init status --wait && printf CLOUD_INIT_DONE")"
    grep -q 'CLOUD_INIT_DONE' <<<"$out"
}

wait_for_new_boot_id() {
    local previous=$1
    local deadline=$((SECONDS + WAIT_SECONDS))
    local current
    while (( SECONDS < deadline )); do
        current="$(qm guest exec "$VMID" -- /bin/cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
        if [[ -n "$current" && "$current" != "$previous" ]]; then
            return 0
        fi
        sleep 5
    done
    return 1
}

wait_for_stopped() {
    local deadline=$((SECONDS + 300))
    while (( SECONDS < deadline )); do
        if [[ "$(qm status "$VMID" | awk '{print $2}')" == "stopped" ]]; then
            return 0
        fi
        sleep 3
    done
    return 1
}

[[ $EUID -eq 0 ]] || die "Run this script as root on the Proxmox host"
for cmd in qm pvesm sha512sum awk grep sed; do
    require_cmd "$cmd"
done

[[ "$VMID" =~ ^[0-9]+$ ]] || die "VMID must be numeric"
[[ "$TEMPLATE_VERSION" =~ ^[0-9]+$ ]] || die "TEMPLATE_VERSION must be numeric"
[[ "$WAIT_SECONDS" =~ ^[0-9]+$ ]] || die "WAIT_SECONDS must be numeric"

vm_exists && die "VMID ${VMID} already exists. Refusing to overwrite or destroy it."
pvesm status --storage "$DISK_STORAGE" >/dev/null 2>&1 || die "Storage '${DISK_STORAGE}' is not available"
pvesm status --storage "$SNIPPET_STORAGE" >/dev/null 2>&1 || die "Snippet storage '${SNIPPET_STORAGE}' is not available"

if ! SNIPPET_PATH="$(pvesm path "$SNIPPET_VOL" 2>/dev/null)"; then
    die "Storage '${SNIPPET_STORAGE}' is not enabled for snippets. Enable the 'Snippets' content type in Proxmox storage settings and rerun."
fi

[[ ! -e "$SNIPPET_PATH" ]] || die "Temporary snippet already exists: ${SNIPPET_PATH}"
mkdir -p "$(dirname "$SNIPPET_PATH")" "$IMAGE_DIR"

log "Downloading Debian 13 generic cloud image"
fetch_file "$IMAGE_URL" "$IMAGE_PATH"
fetch_file "$CHECKSUM_URL" "$CHECKSUM_PATH"

log "Verifying SHA-512 checksum"
checksum_line="$(awk -v f="$IMAGE_NAME" '$2 == f || $2 == ("*" f) {print; exit}' "$CHECKSUM_PATH")"
[[ -n "$checksum_line" ]] || die "No checksum entry found for ${IMAGE_NAME} in ${CHECKSUM_URL}"
IMAGE_SHA512="$(awk '{print $1}' <<<"$checksum_line")"
[[ "$IMAGE_SHA512" =~ ^[0-9a-fA-F]{128}$ ]] || die "Invalid SHA-512 checksum entry for ${IMAGE_NAME}"
(
    cd "$IMAGE_DIR"
    printf '%s\n' "$checksum_line" | sha512sum --check --strict -
)

log "Creating temporary Cloud-Init bootstrap"
cat >"$SNIPPET_PATH" <<'CLOUDCFG'
#cloud-config
hostname: builder-debian13
manage_etc_hosts: true
timezone: Europe/Moscow
ssh_pwauth: false
disable_root: true

users:
  - default
  - name: ops
    gecos: Infrastructure administrator
    groups: [adm, sudo]
    shell: /bin/bash
    lock_passwd: true

write_files:
  - path: /etc/ssh/sshd_config.d/00-template-security.conf
    owner: root:root
    permissions: '0644'
    content: |
      PermitRootLogin no
      PasswordAuthentication no
      KbdInteractiveAuthentication no
      PermitEmptyPasswords no
      PubkeyAuthentication yes

  - path: /etc/systemd/system/getty@tty1.service.d/autologin.conf
    owner: root:root
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin ops --noclear %I $TERM

  - path: /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
    owner: root:root
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin ops --noclear --keep-baud 115200,57600,38400,9600 %I $TERM

  - path: /etc/sudoers.d/90-ops
    owner: root:root
    permissions: '0440'
    content: |
      ops ALL=(ALL:ALL) NOPASSWD:ALL

  - path: /etc/profile.d/99-admin-history.sh
    owner: root:root
    permissions: '0644'
    content: |
      export HISTTIMEFORMAT='%F %T '
      export HISTSIZE=10000
      export HISTFILESIZE=20000

  - path: /etc/motd
    owner: root:root
    permissions: '0644'
    content: |
      Managed VM
      Base template: tpl-debian13
      Infrastructure: zsergeyru/proxmox
      Do not store secrets in Git.

  - path: /usr/local/sbin/template-bootstrap
    owner: root:root
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -Eeuo pipefail
      export DEBIAN_FRONTEND=noninteractive

      apt-get update
      apt-get -y full-upgrade
      apt-get install -y --no-install-recommends \
        qemu-guest-agent openssh-server sudo locales cloud-guest-utils systemd-timesyncd \
        linux-image-amd64 console-setup console-setup-linux \
        git mc nano \
        curl wget jq ca-certificates openssl \
        htop ncdu lsof tree tmux bash-completion \
        tar rsync zstd unzip acl \
        dnsutils iproute2 iputils-ping net-tools \
        cron logrotate

      cat >/etc/default/console-setup <<'EOF'
      ACTIVE_CONSOLES="/dev/tty[1-6]"
      CHARMAP="UTF-8"
      CODESET="CyrSlav"
      FONTFACE="Fixed"
      FONTSIZE="8x16"
      VIDEOMODE=
      EOF
      setupcon --save-only

      mapfile -t cloud_kernel_packages < <(
        dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' 'linux-image-*cloud-amd64' 2>/dev/null \
          | awk '$1 == "ii" {print $2}'
      )
      if (( ${#cloud_kernel_packages[@]} )); then
        apt-get purge -y "${cloud_kernel_packages[@]}"
      fi
      update-grub

      dpkg-query -W -f='${db:Status-Abbrev}\n' linux-image-amd64 | grep -q '^ii'
      if dpkg -l 'linux-image-*cloud-amd64' 2>/dev/null | grep -q '^ii'; then
        printf 'Cloud kernel package is still installed after purge\n' >&2
        exit 1
      fi
      compgen -G '/boot/vmlinuz-*-amd64' >/dev/null

      sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
      locale-gen en_US.UTF-8
      update-locale LANG=en_US.UTF-8
      timedatectl set-timezone Europe/Moscow
      systemctl enable --now systemd-timesyncd.service
      timedatectl set-ntp true

      passwd -l root >/dev/null

      systemctl daemon-reload
      systemctl enable --now qemu-guest-agent
      systemctl enable --now ssh
      systemctl enable getty@tty1.service
      systemctl restart getty@tty1.service
      systemctl enable serial-getty@ttyS0.service
      systemctl restart serial-getty@ttyS0.service
      systemctl enable --now fstrim.timer

      /usr/sbin/sshd -t
      sshd_effective="$(/usr/sbin/sshd -T -C user=ops,host=localhost,addr=127.0.0.1)"
      grep -q '^permitrootlogin no$' <<<"$sshd_effective"
      grep -q '^passwordauthentication no$' <<<"$sshd_effective"
      grep -q '^kbdinteractiveauthentication no$' <<<"$sshd_effective"
      grep -q '^permitemptypasswords no$' <<<"$sshd_effective"
      grep -q '^pubkeyauthentication yes$' <<<"$sshd_effective"
      /usr/sbin/visudo -cf /etc/sudoers.d/90-ops
      id ops >/dev/null
      passwd -S ops | grep -q ' L '
      passwd -S root | grep -q ' L '

      cat >/etc/vm-template-info <<EOF
      Template: tpl-debian13
      Template-Version: __TEMPLATE_VERSION__
      OS: Debian 13
      Kernel-Flavor: amd64
      Primary-Console: VGA/noVNC tty1 autologin ops
      Fallback-Console: serial0 ttyS0 autologin ops
      Infrastructure-Source: zsergeyru/proxmox
      Template-Builder-Source: zsergeyru/proxmox
      Source-Image: __IMAGE_NAME__
      Source-Image-SHA512: __IMAGE_SHA512__
      Build-Date: $(date -u +%F)
      EOF

      mkdir -p /var/lib/template-build
      printf 'BOOTSTRAP_OK\n' >/var/lib/template-build/bootstrap-complete

  - path: /usr/local/sbin/template-finalize
    owner: root:root
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -Eeuo pipefail

      if id debian >/dev/null 2>&1; then
        userdel -r debian || true
      fi

      rm -f /var/lib/template-build/bootstrap-complete
      cloud-init clean --logs --seed

      truncate -s 0 /etc/machine-id
      rm -f /var/lib/dbus/machine-id
      rm -f /etc/ssh/ssh_host_*
      rm -rf /var/lib/dhcp/* /var/lib/NetworkManager/*
      rm -f /var/lib/systemd/random-seed

      apt-get clean
      rm -rf /var/lib/apt/lists/*
      journalctl --rotate || true
      journalctl --vacuum-time=1s || true
      rm -rf /tmp/* /var/tmp/*
      rm -f /root/.bash_history /home/ops/.bash_history
      rm -rf /home/ops/.ssh
      rm -rf /var/lib/template-build
      rm -f /usr/local/sbin/template-bootstrap /usr/local/sbin/template-finalize

      fstrim -av || printf 'WARNING: fstrim did not complete successfully; continuing template finalization\n' >&2
      sync
      printf 'FINALIZE_OK\n'

runcmd:
  - [bash, -lc, /usr/local/sbin/template-bootstrap]
CLOUDCFG

sed -i \
    -e "s/__TEMPLATE_VERSION__/${TEMPLATE_VERSION}/g" \
    -e "s/__IMAGE_NAME__/${IMAGE_NAME}/g" \
    -e "s/__IMAGE_SHA512__/${IMAGE_SHA512}/g" \
    "$SNIPPET_PATH"

log "Creating builder VM ${VMID}"
qm create "$VMID" \
    --name "$BUILDER_NAME" \
    --ostype l26 \
    --cpu host \
    --sockets 1 \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=${BRIDGE}" \
    --serial0 socket \
    --vga std \
    --agent 1 \
    --onboot 0

log "Importing system disk"
qm importdisk "$VMID" "$IMAGE_PATH" "$DISK_STORAGE"
IMPORTED_DISK="$(qm config "$VMID" | awk -F': ' '/^unused[0-9]+:/ {print $2; exit}')"
[[ -n "$IMPORTED_DISK" ]] || die "Imported disk was not found in VM configuration"

qm set "$VMID" --scsi0 "${IMPORTED_DISK},discard=on,iothread=1,ssd=1"
qm resize "$VMID" scsi0 "$DISK_SIZE"
qm set "$VMID" --ide2 "${DISK_STORAGE}:cloudinit"
qm set "$VMID" --boot "order=scsi0"
qm set "$VMID" --ipconfig0 ip=dhcp
qm set "$VMID" --cicustom "user=${SNIPPET_VOL}"

log "Starting builder VM"
qm start "$VMID"

log "Waiting for QEMU Guest Agent"
wait_for_agent || die "QEMU Guest Agent did not become available within ${WAIT_SECONDS}s"

log "Waiting for bootstrap to complete"
wait_for_bootstrap || die "Guest bootstrap did not finish within ${WAIT_SECONDS}s. Inspect VM ${VMID} console and cloud-init logs."

log "Waiting for Cloud-Init final stage"
wait_for_cloud_init || die "Cloud-Init did not reach the done state cleanly"

log "Rebooting builder into regular Debian kernel"
BOOT_ID_BEFORE="$(qm guest exec "$VMID" -- /bin/cat /proc/sys/kernel/random/boot_id)"
qm reboot "$VMID"
wait_for_new_boot_id "$BOOT_ID_BEFORE" || die "Builder VM did not complete the verification reboot within ${WAIT_SECONDS}s"
wait_for_agent || die "QEMU Guest Agent did not reconnect after the verification reboot"

log "Verifying kernel, framebuffer and console services after reboot"
VERIFY_OUTPUT="$(qm guest exec "$VMID" -- /bin/bash -lc '
set -Eeuo pipefail
kernel="$(uname -r)"
[[ "$kernel" == *-amd64 ]]
[[ "$kernel" != *cloud* ]]
[[ -r /sys/class/graphics/fb0/virtual_size ]]
framebuffer="$(cat /sys/class/graphics/fb0/virtual_size)"
[[ -n "$framebuffer" ]]
[[ "$framebuffer" =~ ^[0-9]+,[0-9]+$ ]]
systemctl is-active --quiet qemu-guest-agent.service
systemctl is-active --quiet getty@tty1.service
systemctl is-active --quiet serial-getty@ttyS0.service
grep -q "^FONTSIZE=\"8x16\"$" /etc/default/console-setup
grep -q -- "--autologin ops" /etc/systemd/system/getty@tty1.service.d/autologin.conf
grep -q -- "--autologin ops" /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
printf "VERIFY_KERNEL=%s VERIFY_FRAMEBUFFER=%s CONSOLES_OK\n" "$kernel" "$framebuffer"
')"
grep -q 'CONSOLES_OK' <<<"$VERIFY_OUTPUT" || die "Post-reboot console verification did not report success"
KERNEL_TOKEN="$(grep -oE 'VERIFY_KERNEL=[^[:space:]\"]+' <<<"$VERIFY_OUTPUT" | head -1)"
FRAMEBUFFER_TOKEN="$(grep -oE 'VERIFY_FRAMEBUFFER=[0-9]+,[0-9]+' <<<"$VERIFY_OUTPUT" | head -1)"
KERNEL_VERSION="${KERNEL_TOKEN#*=}"
FRAMEBUFFER_SIZE="${FRAMEBUFFER_TOKEN#*=}"
[[ -n "$KERNEL_VERSION" ]] || die "Verified kernel version was not reported"
[[ -n "$FRAMEBUFFER_SIZE" ]] || die "Verified framebuffer size was not reported"

log "Running final cleanup inside the guest"
FINALIZE_OUTPUT="$(qm guest exec "$VMID" -- /usr/local/sbin/template-finalize)"
grep -q 'FINALIZE_OK' <<<"$FINALIZE_OUTPUT" || die "Guest finalization did not report success"

log "Shutting down builder VM"
qm shutdown "$VMID" --timeout 180 || true
wait_for_stopped || die "Builder VM did not stop cleanly"

log "Removing builder-only Cloud-Init and setting clone defaults"
qm set "$VMID" --delete cicustom
qm set "$VMID" --ciuser ops
qm set "$VMID" --ciupgrade 0
qm set "$VMID" --ipconfig0 ip=dhcp
qm set "$VMID" --name "$TEMPLATE_NAME"
qm set "$VMID" --description "Debian 13 (Trixie) base template; version ${TEMPLATE_VERSION}; VGA/noVNC tty1 autologin; serial0 fallback; SSH keys supplied per clone"

log "Regenerating standard Proxmox Cloud-Init drive"
qm cloudinit update "$VMID"
CLOUDINIT_USER_DATA="$(qm cloudinit dump "$VMID" user)"
if grep -qE 'template-bootstrap|builder-debian13|/usr/local/sbin/template-finalize' <<<"$CLOUDINIT_USER_DATA"; then
    die "Builder-only Cloud-Init content is still present after regeneration"
fi

rm -f "$SNIPPET_PATH"

log "Converting VM ${VMID} to template"
qm template "$VMID"

log "Protecting base template from accidental deletion"
qm set "$VMID" --protection 1

FINAL_CONFIG="$(qm config "$VMID")"
grep -q '^template: 1$' <<<"$FINAL_CONFIG" || die "VM ${VMID} was not marked as a template"
grep -q '^protection: 1$' <<<"$FINAL_CONFIG" || die "Template protection was not enabled"
grep -q '^agent: 1$' <<<"$FINAL_CONFIG" || die "QEMU Guest Agent support is not enabled in template config"
grep -q '^vga: std$' <<<"$FINAL_CONFIG" || die "Template VGA display is not set to std"
grep -q '^serial0: socket$' <<<"$FINAL_CONFIG" || die "Template serial0 is not configured as socket"
grep -q '^ciuser: ops$' <<<"$FINAL_CONFIG" || die "Template Cloud-Init user is not ops"
grep -q '^ciupgrade: 0$' <<<"$FINAL_CONFIG" || die "Cloud-Init automatic package upgrade is not disabled"
grep -q '^ipconfig0: ip=dhcp$' <<<"$FINAL_CONFIG" || die "Template default network is not DHCP"
if grep -q '^cicustom:' <<<"$FINAL_CONFIG"; then
    die "Builder-only cicustom is still present in template configuration"
fi

trap - ERR

cat <<EOF

Template created successfully.

VMID:        ${VMID}
Name:        ${TEMPLATE_NAME}
Version:     ${TEMPLATE_VERSION}
Storage:     ${DISK_STORAGE}
Network:     DHCP by default
User:        ops (password locked)
Root:        password locked; SSH login disabled
Console:     Proxmox noVNC / VGA tty1 autologin as ops
Serial:      serial0 / ttyS0 autologin as ops (fallback)
Kernel:      ${KERNEL_VERSION}
Framebuffer: ${FRAMEBUFFER_SIZE}
SSH:         inject one or more public keys on each clone through Cloud-Init
Updates:     automatic Cloud-Init package upgrade disabled (ciupgrade=0)
Protect:     Proxmox protection enabled on the base template
Image:       ${IMAGE_NAME}
SHA-512:     ${IMAGE_SHA512}

Recommended next step: create a FULL clone, set SSH public key/network before the
first start, regenerate its Cloud-Init drive, then verify noVNC/tty1 autologin,
serial0 fallback, QEMU Guest Agent, SSH access, unique machine-id/host keys,
regular amd64 kernel, framebuffer availability and filesystem growth.

EOF
