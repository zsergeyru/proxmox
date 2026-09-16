#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

STATUS_DIR=/var/lib/template-build
STATUS_FILE=${STATUS_DIR}/bootstrap-status
mkdir -p "$STATUS_DIR"
status() {
    printf '%s\n' "$1" >"$STATUS_FILE"
}

status 'apt-metadata'
apt-get update

status 'qga-install'
apt-get install -y --no-install-recommends qemu-guest-agent
systemctl enable --now qemu-guest-agent

status 'apt-upgrade'
apt-get -y full-upgrade

status 'base-packages'
apt-get install -y --no-install-recommends \
    openssh-server sudo locales cloud-guest-utils systemd-timesyncd \
    linux-image-amd64 console-setup console-setup-linux \
    git mc nano \
    curl wget jq ca-certificates openssl \
    htop ncdu lsof tree tmux bash-completion \
    tar rsync zstd unzip acl \
    dnsutils iproute2 iputils-ping net-tools \
    cron logrotate

status 'console'
cat >/etc/default/console-setup <<'EOF'
ACTIVE_CONSOLES="/dev/tty[1-6]"
CHARMAP="UTF-8"
CODESET="CyrSlav"
FONTFACE="Fixed"
FONTSIZE="8x16"
VIDEOMODE=
EOF
setupcon --save-only

status 'kernel'
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
    printf 'Пакет cloud-ядра всё ещё установлен после удаления\n' >&2
    exit 1
fi
compgen -G '/boot/vmlinuz-*-amd64' >/dev/null

status 'locale-time'
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
locale-gen en_US.UTF-8
update-locale LANG=en_US.UTF-8
timedatectl set-timezone Europe/Moscow
systemctl enable --now systemd-timesyncd.service
timedatectl set-ntp true

passwd -l root >/dev/null

status 'services'
systemctl daemon-reload
systemctl enable --now qemu-guest-agent
systemctl enable --now ssh
systemctl enable getty@tty1.service
systemctl restart getty@tty1.service
systemctl enable serial-getty@ttyS0.service
systemctl restart serial-getty@ttyS0.service
systemctl enable --now fstrim.timer

status 'security'
/usr/sbin/sshd -t
sshd_effective="$(/usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1)"
grep -Eq '^permitrootlogin (prohibit-password|without-password)$' <<<"$sshd_effective"
grep -q '^passwordauthentication no$' <<<"$sshd_effective"
grep -q '^kbdinteractiveauthentication no$' <<<"$sshd_effective"
grep -q '^permitemptypasswords no$' <<<"$sshd_effective"
grep -q '^pubkeyauthentication yes$' <<<"$sshd_effective"
passwd -S root | grep -q ' L '

status 'metadata'
cat >/etc/vm-template-info <<EOF
Шаблон: tpl-debian13
Версия-шаблона: __TEMPLATE_VERSION__
ОС: Debian 13
Тип-ядра: amd64
Management-user: root
SSH: root key-only
Основная-консоль: VGA/noVNC tty1, автовход root
Резервная-консоль: serial0 ttyS0, автовход root
Источник-инфраструктуры: zsergeyru/proxmox
Источник-сборщика-шаблона: zsergeyru/proxmox
Исходный-образ: __IMAGE_NAME__
SHA512-исходного-образа: __IMAGE_SHA512__
Дата-сборки: $(date -u +%F)
EOF

status 'done'
printf 'BOOTSTRAP_OK\n' >${STATUS_DIR}/bootstrap-complete
