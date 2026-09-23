#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "setup.sh должен выполняться от root"
[[ -r /etc/os-release ]] || die "Не найден /etc/os-release"

# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "13" ]] \
    || die "Ожидается Debian 13"

: "${TEMPLATE_VERSION:?TEMPLATE_VERSION не задан}"
: "${SOURCE_ISO:?SOURCE_ISO не задан}"
: "${SOURCE_ISO_CHECKSUM:?SOURCE_ISO_CHECKSUM не задан}"

apt-get update
apt-get -y full-upgrade
apt-get install -y --no-install-recommends \
    qemu-guest-agent \
    openssh-server \
    cloud-init \
    cloud-guest-utils \
    ifupdown \
    dhcpcd-base \
    ca-certificates \
    curl \
    linux-image-amd64

cat >/etc/ssh/sshd_config.d/90-template.conf <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
EOF

cat >/etc/cloud/cloud.cfg.d/90-proxmox.cfg <<'EOF'
datasource_list: [ NoCloud ]
disable_root: false
ssh_pwauth: false
manage_etc_hosts: true
preserve_hostname: false
EOF

install -d -m 0755 /etc/systemd/system/getty@tty1.service.d
cat >/etc/systemd/system/getty@tty1.service.d/autologin.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
EOF

install -d -m 0755 /etc/systemd/system/serial-getty@ttyS0.service.d
cat >/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --keep-baud 115200,57600,38400,9600 - $TERM
EOF

systemctl daemon-reload
systemctl enable fstrim.timer
systemctl enable getty@tty1.service
systemctl enable serial-getty@ttyS0.service

cat >/etc/vm-template-info <<EOF
Template-Version=${TEMPLATE_VERSION}
Management-user=root
SSH-policy=key-only-after-finalize
Source-ISO=${SOURCE_ISO}
Source-ISO-Checksum=${SOURCE_ISO_CHECKSUM}
Build-Date=$(date -u +%Y-%m-%dT%H:%M:%SZ)
Kernel-Flavor=linux-image-amd64
Console-modes=tty1,ttyS0
EOF
chmod 0644 /etc/vm-template-info

sshd -t
dpkg-query -W -f='${db:Status-Abbrev}\n' linux-image-amd64 | grep -q '^ii'
