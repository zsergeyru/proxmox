#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

STATUS_DIR=/var/lib/template-build
STATUS_FILE=${STATUS_DIR}/bootstrap-status
mkdir -p "$STATUS_DIR"
status() {
    printf '%s\n' "$1" >"$STATUS_FILE"
}

cat >/usr/local/sbin/guest-status <<'EOF_GUEST_STATUS'
#!/usr/bin/env bash
set -u

STATUS_FILE=/var/lib/template-build/bootstrap-status

stage_label() {
    case "${1:-}" in
        apt-metadata) printf '%s' 'APT metadata refresh' ;;
        qga-install) printf '%s' 'QEMU Guest Agent installation' ;;
        apt-upgrade) printf '%s' 'APT full-upgrade' ;;
        base-packages) printf '%s' 'Base packages installation' ;;
        console) printf '%s' 'Console configuration' ;;
        kernel) printf '%s' 'Regular Debian kernel setup' ;;
        locale-time) printf '%s' 'Locale, timezone and time sync' ;;
        services) printf '%s' 'SSH, QGA, console and fstrim services' ;;
        security) printf '%s' 'SSH and root security checks' ;;
        metadata) printf '%s' 'Template metadata write' ;;
        done) printf '%s' 'Initial template bootstrap complete' ;;
        '') printf '%s' 'No active template build stage' ;;
        *) printf 'Unknown stage (%s)' "$1" ;;
    esac
}

stage=''
[[ -r "$STATUS_FILE" ]] && stage="$(head -n1 "$STATUS_FILE" 2>/dev/null || true)"

qga_package='not-installed'
if dpkg-query -W -f='${db:Status-Abbrev}' qemu-guest-agent 2>/dev/null | grep -q '^ii'; then
    qga_package='installed'
fi

qga_service="$(systemctl is-active qemu-guest-agent.service 2>/dev/null || true)"
[[ -n "$qga_service" ]] || qga_service='unknown'
qga_enabled="$(systemctl is-enabled qemu-guest-agent.service 2>/dev/null || true)"
[[ -n "$qga_enabled" ]] || qga_enabled='unknown'

qga_channel='missing'
[[ -e /dev/virtio-ports/org.qemu.guest_agent.0 ]] && qga_channel='present'

cloud_init='unavailable'
if command -v cloud-init >/dev/null 2>&1; then
    cloud_init="$(cloud-init status 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/[[:space:]]$//' || true)"
    [[ -n "$cloud_init" ]] || cloud_init='unknown'
fi

ips="$(hostname -I 2>/dev/null | sed 's/[[:space:]]\+$//' || true)"
[[ -n "$ips" ]] || ips='none'

uptime_seconds='unknown'
if [[ -r /proc/uptime ]]; then
    read -r uptime_raw _ </proc/uptime || true
    uptime_seconds="${uptime_raw%%.*}"
fi

printf '%s\n' 'Guest / template diagnostics'
printf '%s\n' '----------------------------'
printf 'Build stage code : %s\n' "${stage:-none}"
printf 'Build stage      : %s\n' "$(stage_label "$stage")"
printf 'QGA package      : %s\n' "$qga_package"
printf 'QGA service      : %s\n' "$qga_service"
printf 'QGA enabled      : %s\n' "$qga_enabled"
printf 'QGA virtio port  : %s\n' "$qga_channel"
printf 'Cloud-Init       : %s\n' "$cloud_init"
printf 'IP addresses     : %s\n' "$ips"
printf 'Uptime           : %ss\n' "$uptime_seconds"
printf '\nRun again at any time: guest-status\n'
EOF_GUEST_STATUS
chmod 0755 /usr/local/sbin/guest-status

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
