#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "cleanup.sh должен выполняться от root"

rm -f /etc/ssh/sshd_config.d/00-packer-build.conf
cat >/etc/ssh/sshd_config.d/90-template.conf <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
EOF

sshd -t
passwd -l root >/dev/null
passwd -S root | awk '$2 == "L" {ok=1} END {exit ok ? 0 : 1}' \
    || die "Пароль root не заблокирован"

rm -rf /root/.ssh
rm -f /etc/ssh/ssh_host_*

cloud-init clean --logs
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

apt-get clean
rm -rf /var/lib/apt/lists/*
find /var/log -type f -exec truncate -s 0 {} +
rm -rf /tmp/* /var/tmp/*

fstrim -av || true
sync

[[ ! -e /root/.ssh ]] || die "/root/.ssh не удалён"
[[ ! -e /var/lib/dbus/machine-id ]] || die "/var/lib/dbus/machine-id не удалён"
[[ ! -s /etc/machine-id ]] || die "/etc/machine-id должен быть пустым"
if compgen -G '/etc/ssh/ssh_host_*' >/dev/null; then
    die "SSH host keys не удалены"
fi

sshd -t
