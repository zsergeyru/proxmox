#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "cleanup.sh должен выполняться от root"

# Установщик временно разрешает пароль root только для подключения Packer.
rm -f /etc/ssh/sshd_config.d/00-packer-build.conf
sshd -t

passwd -l root >/dev/null
passwd -S root | awk '$2 == "L" {ok=1} END {exit ok ? 0 : 1}' \
    || die "Пароль root не заблокирован"

# Конфигурацию сети конкретной VM должен создать Cloud-Init после клонирования.
cat >/etc/network/interfaces <<'EOF'
auto lo
iface lo inet loopback

source /etc/network/interfaces.d/*
EOF
rm -f /etc/network/interfaces.d/*
rm -f /var/lib/dhcp/* 2>/dev/null || true

rm -rf /root/.ssh
rm -f /etc/ssh/ssh_host_*

cloud-init clean --logs --seed
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /tmp/* /var/tmp/*
find /var/log -type f -exec truncate -s 0 {} +

fstrim -av || true
sync

[[ ! -e /root/.ssh ]] || die "/root/.ssh не удалён"
[[ ! -e /var/lib/dbus/machine-id ]] || die "/var/lib/dbus/machine-id не удалён"
[[ ! -s /etc/machine-id ]] || die "/etc/machine-id должен быть пустым"
if compgen -G '/etc/ssh/ssh_host_*' >/dev/null; then
    die "SSH host keys не удалены"
fi

