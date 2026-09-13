#!/usr/bin/env bash
set -Eeuo pipefail

# Debian 13 (Trixie) base VM template builder for Proxmox VE.
#
# Run on a Proxmox VE host as root.
#
# Examples:
#   bash templates/debian13/create-template.sh
#   STORAGE=local-zfs VMID=9100 bash templates/debian13/create-template.sh
#   AUTO_INSTALL_BUILD_DEPS=1 bash templates/debian13/create-template.sh

VMID="${VMID:-9000}"
VM_NAME="${VM_NAME:-tpl-debian13}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
CORES="${CORES:-1}"
MEMORY="${MEMORY:-1024}"
DISK_SIZE="${DISK_SIZE:-16G}"
CIUSER="${CIUSER:-debian}"
TIMEZONE="${TIMEZONE:-Etc/UTC}"

IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
AUTO_INSTALL_BUILD_DEPS="${AUTO_INSTALL_BUILD_DEPS:-0}"
KEEP_WORKDIR="${KEEP_WORKDIR:-0}"

PACKAGES=(
  qemu-guest-agent
  openssh-server
  sudo
  git
  mc
  nano
  curl
  wget
  jq
  ca-certificates
  openssl
  htop
  ncdu
  lsof
  tree
  tmux
  bash-completion
  tar
  rsync
  restic
  zstd
  unzip
  acl
  dnsutils
  iproute2
  iputils-ping
  net-tools
  cron
  logrotate
)

log() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local rc=$?
  if [[ -n "${WORKDIR:-}" && -d "${WORKDIR:-}" ]]; then
    if [[ "$KEEP_WORKDIR" == "1" || $rc -ne 0 ]]; then
      printf '\nWork directory kept for inspection: %s\n' "$WORKDIR" >&2
    else
      rm -rf "$WORKDIR"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script as root on the Proxmox VE host."
}

require_pve() {
  command -v qm >/dev/null 2>&1 || die "'qm' not found. Run this script on a Proxmox VE host."
  command -v pvesm >/dev/null 2>&1 || die "'pvesm' not found. Run this script on a Proxmox VE host."
}

install_build_deps_if_requested() {
  local missing=0
  for cmd in virt-customize qemu-img curl sha512sum; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing=1
    fi
  done

  if [[ "$missing" -eq 0 ]]; then
    return
  fi

  if [[ "$AUTO_INSTALL_BUILD_DEPS" != "1" ]]; then
    cat >&2 <<'EOF'

Build dependencies are missing.

Install them once on the Proxmox host:
  apt-get update
  apt-get install -y libguestfs-tools dhcpcd-base curl

Or allow this script to install them:
  AUTO_INSTALL_BUILD_DEPS=1 bash templates/debian13/create-template.sh

dhcpcd-base is used because virt-customize networking can otherwise fail on
some Proxmox VE 9 installations. It does not install the dhcpcd service.
EOF
    exit 1
  fi

  log "Installing build dependencies on the Proxmox host"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    libguestfs-tools dhcpcd-base curl
}

validate_inputs() {
  [[ "$VMID" =~ ^[0-9]+$ ]] || die "VMID must be numeric."
  [[ "$CORES" =~ ^[0-9]+$ ]] || die "CORES must be numeric."
  [[ "$MEMORY" =~ ^[0-9]+$ ]] || die "MEMORY must be numeric."
  [[ "$DISK_SIZE" =~ ^[0-9]+[KMGT]$ ]] || die "DISK_SIZE must look like 16G, 32G, etc."

  if qm status "$VMID" >/dev/null 2>&1; then
    die "VMID $VMID already exists. Refusing to overwrite it."
  fi

  pvesm config "$STORAGE" >/dev/null 2>&1 \
    || die "Storage '$STORAGE' does not exist. Check with: pvesm status"

  local content
  content="$(pvesm config "$STORAGE" | awk '/^content / {print $2}')"
  [[ ",${content}," == *",images,"* ]] \
    || die "Storage '$STORAGE' does not allow VM disk images (content=images)."

  ip link show "$BRIDGE" >/dev/null 2>&1 \
    || die "Bridge '$BRIDGE' was not found."

  case "$IMAGE_URL" in
    https://*.qcow2) ;;
    *) die "IMAGE_URL must be an HTTPS URL ending in .qcow2." ;;
  esac
}

download_and_verify_image() {
  WORKDIR="$(mktemp -d /var/tmp/proxmox-debian13-template.XXXXXX)"
  IMAGE_NAME="$(basename "$IMAGE_URL")"
  IMAGE_DIR_URL="${IMAGE_URL%/*}"
  BASE_IMAGE="${WORKDIR}/${IMAGE_NAME}"
  CHECKSUMS="${WORKDIR}/SHA512SUMS"
  CUSTOM_IMAGE="${WORKDIR}/debian13-custom.qcow2"
  COMPACT_IMAGE="${WORKDIR}/debian13-custom-compact.qcow2"

  log "Downloading Debian 13 genericcloud image"
  curl --fail --location --retry 3 --retry-delay 2 \
    "$IMAGE_URL" -o "$BASE_IMAGE"

  log "Downloading SHA512SUMS"
  curl --fail --location --retry 3 --retry-delay 2 \
    "${IMAGE_DIR_URL}/SHA512SUMS" -o "$CHECKSUMS"

  local expected
  expected="$(
    awk -v f="$IMAGE_NAME" '
      {
        name=$2
        sub(/^\*/, "", name)
        sub(/^\.\//, "", name)
        if (name == f) {
          print $1
          exit
        }
      }
    ' "$CHECKSUMS"
  )"

  [[ -n "$expected" ]] \
    || die "Could not find $IMAGE_NAME in SHA512SUMS."

  printf '%s  %s\n' "$expected" "$BASE_IMAGE" | sha512sum --check --status \
    || die "SHA512 checksum verification failed."

  log "Checksum verified"
  cp --reflink=auto "$BASE_IMAGE" "$CUSTOM_IMAGE"
}

customize_image() {
  local packages_csv
  packages_csv="$(IFS=,; echo "${PACKAGES[*]}")"

  log "Updating Debian image and installing base packages"
  virt-customize \
    --network \
    -a "$CUSTOM_IMAGE" \
    --update \
    --install "$packages_csv" \
    --timezone "$TIMEZONE" \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable ssh.service' \
    --run-command 'apt-get clean' \
    --run-command 'rm -rf /var/lib/apt/lists/*' \
    --run-command 'rm -f /etc/ssh/ssh_host_*' \
    --run-command 'cloud-init clean --logs --machine-id || cloud-init clean --logs || true' \
    --truncate /etc/machine-id

  log "Compacting customized QCOW2 image"
  qemu-img convert \
    -O qcow2 \
    -c \
    -o preallocation=off \
    "$CUSTOM_IMAGE" "$COMPACT_IMAGE"

  qemu-img check "$COMPACT_IMAGE" >/dev/null
}

create_vm_template() {
  log "Creating Proxmox VM $VMID ($VM_NAME)"
  qm create "$VMID" \
    --name "$VM_NAME" \
    --ostype l26 \
    --cpu host \
    --cores "$CORES" \
    --memory "$MEMORY" \
    --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=${BRIDGE}" \
    --agent enabled=1 \
    --serial0 socket \
    --vga serial0 \
    --onboot 0

  log "Importing system disk into storage '$STORAGE'"
  qm disk import "$VMID" "$COMPACT_IMAGE" "$STORAGE"

  local disk_volume
  disk_volume="$(
    qm config "$VMID" \
      | sed -n 's/^unused0: \([^,]*\).*/\1/p'
  )"

  [[ -n "$disk_volume" ]] \
    || die "Imported disk was not found as unused0 in VM $VMID."

  qm set "$VMID" \
    --scsi0 "${disk_volume},discard=on,iothread=1"

  log "Resizing system disk to $DISK_SIZE"
  qm disk resize "$VMID" scsi0 "$DISK_SIZE"

  log "Adding Cloud-Init drive and defaults"
  qm set "$VMID" --ide2 "${STORAGE}:cloudinit"
  qm set "$VMID" --boot order=scsi0
  qm set "$VMID" --ciuser "$CIUSER"
  qm set "$VMID" --ciupgrade 0
  qm set "$VMID" --ipconfig0 ip=dhcp

  log "Converting VM $VMID to template"
  qm template "$VMID"
}

print_summary() {
  cat <<EOF

Template created successfully.

  VMID:        $VMID
  Name:        $VM_NAME
  OS:          Debian 13 (Trixie)
  Storage:     $STORAGE
  Bridge:      $BRIDGE
  CPU:         $CORES vCPU, type=host
  RAM:         ${MEMORY} MiB
  Disk:        $DISK_SIZE
  Cloud-Init:  enabled
  Guest Agent: installed + enabled
  Network:     DHCP
  CI user:     $CIUSER

Example full clone:
  qm clone $VMID 101 --name network-gateway --full 1

After cloning, set CPU/RAM/IP/SSH key for the concrete VM before first boot.
EOF
}

main() {
  require_root
  require_pve
  install_build_deps_if_requested
  validate_inputs
  download_and_verify_image
  customize_image
  create_vm_template
  print_summary
}

main "$@"
