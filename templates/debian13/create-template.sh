#!/usr/bin/env bash
set -Eeuo pipefail

# Build the canonical Debian 13 Proxmox template without libguestfs/virt-customize.
# The generic cloud image is booted as VMID 9000, configured inside the guest
# through temporary Cloud-Init user-data, finalized through QEMU Guest Agent,
# and then converted to a Proxmox template.
#
# Access model:
#   - ops has no usable password;
#   - SSH password authentication is disabled;
#   - SSH public keys are supplied per clone through Proxmox Cloud-Init;
#   - tty1 uses autologin for ops and is exposed through the trusted Proxmox
#     noVNC console;
#   - serial0 remains available as a diagnostic channel without autologin.

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
TEMPLATE_VERSION="${TEMPLATE_VERSION:-2}"

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
locale: en_US.UTF-8
ssh_pwauth: false
disable_root: true

users:
  - default
  - name: ops
    gecos: Infrastructure administrator
    groups: [adm, sudo]
    shell: /bin/bash
    lock_passwd: true
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]

write_files:
  - path: /etc/ssh/sshd_config.d/99-template-security.conf
    owner: root:root
    permissions: '0644'
    content: |
      PermitRootLogin no
      PasswordAuthentication no
      KbdInteractiveAuthentication no
      PubkeyAuthentication yes

  - path: /etc/systemd/system/getty@tty1.service.d/autologin.conf
    owner: root:root
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin ops --noclear %I $TERM

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
        qemu-guest-agent openssh-server sudo locales \
        git mc nano \
        curl wget jq ca-certificates openssl \
        htop ncdu lsof tree tmux bash-completion \
        tar rsync restic zstd unzip acl \
        dnsutils iproute2 iputils-ping net-tools \
        cron logrotate

      sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
      locale-gen en_US.UTF-8
      update-locale LANG=en_US.UTF-8
      timedatectl set-timezone Europe/Moscow
      timedatectl set-ntp true || true

      systemctl daemon-reload
      systemctl enable --now qemu-guest-agent
      systemctl enable ssh
      systemctl restart ssh
      systemctl enable getty@tty1.service
      systemctl enable serial-getty@ttyS0.service || true

      if id debian >/dev/null 2>&1; then
        userdel -r debian || true
      fi

      cat >/etc/vm-template-info <<EOF
      Template: tpl-debian13
      Template-Version: 2
      OS: Debian 13
      Source: zsergeyru/proxmox
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

      rm -f /var/lib/template-build/bootstrap-complete
      cloud-init clean --logs --seed || true

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
      sync
      printf 'FINALIZE_OK\n'

runcmd:
  - [bash, -lc, /usr/local/sbin/template-bootstrap]
CLOUDCFG

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

log "Running final cleanup inside the guest"
FINALIZE_OUTPUT="$(qm guest exec "$VMID" -- /usr/local/sbin/template-finalize)"
grep -q 'FINALIZE_OK' <<<"$FINALIZE_OUTPUT" || die "Guest finalization did not report success"

log "Shutting down builder VM"
qm shutdown "$VMID" --timeout 180 || true
wait_for_stopped || die "Builder VM did not stop cleanly"

log "Removing builder-only Cloud-Init and setting clone defaults"
qm set "$VMID" --delete cicustom
qm set "$VMID" --ciuser ops
qm set "$VMID" --ipconfig0 ip=dhcp
qm set "$VMID" --name "$TEMPLATE_NAME"
qm set "$VMID" --description "Debian 13 (Trixie) base template; version ${TEMPLATE_VERSION}; Proxmox tty1 autologin; SSH keys supplied per clone"

rm -f "$SNIPPET_PATH"
trap - ERR

log "Converting VM ${VMID} to template"
qm template "$VMID"

cat <<EOF

Template created successfully.

VMID:    ${VMID}
Name:    ${TEMPLATE_NAME}
Version: ${TEMPLATE_VERSION}
Storage: ${DISK_STORAGE}
Network: DHCP by default
User:    ops (password locked)
Console: Proxmox noVNC / tty1 autologin as ops
Serial:  serial0 kept as a diagnostic channel without autologin
SSH:     inject one or more public keys on each clone through Cloud-Init

Recommended next step: create a FULL clone, configure its SSH public key through
Cloud-Init, then verify noVNC tty1 autologin, QEMU Guest Agent and SSH access.
EOF
