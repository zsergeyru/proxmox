#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# shellcheck source=/dev/null
source "${ROOT}/scripts/pve/setup/lib/00-common.sh"
# shellcheck source=/dev/null
source "${ROOT}/scripts/pve/setup/lib/60-template-contract.sh"

valid_config='name: tpl-debian13
template: 1
ostype: l26
cpu: host
sockets: 1
cores: 1
memory: 1024
scsihw: virtio-scsi-single
scsi0: local-lvm:base-9000-disk-0,discard=on,iothread=1,size=16G,ssd=1
ide2: local-lvm:vm-9000-cloudinit,media=cdrom
net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0
boot: order=scsi0
onboot: 0
ciuser: root
ciupgrade: 0
agent: 1
vga: std
serial0: socket
ipconfig0: ip=dhcp
description: template-version=7
protection: 1'

validate_template_contract "$valid_config" 1

expect_invalid() {
    local label=$1 config=$2
    if validate_template_contract "$config" 1 >/dev/null 2>&1; then
        printf 'Expected invalid template contract: %s\n' "$label" >&2
        exit 1
    fi
}

expect_invalid "wrong bridge" "${valid_config/bridge=vmbr0/bridge=vmbr9}"
expect_invalid "undersized disk" "${valid_config/size=16G/size=8G}"
expect_invalid "missing cloud-init media" "${valid_config/media=cdrom/media=disk}"
expect_invalid "ordinary cdrom instead of cloud-init" "${valid_config/vm-9000-cloudinit/debian-installer.iso}"
expect_invalid "builder cicustom leaked" "${valid_config}"$'\n''cicustom: user=local:snippets/builder.yaml'
expect_invalid "wrong scsi controller" "${valid_config/virtio-scsi-single/virtio-scsi-pci}"

printf 'Template contract tests passed.\n'
