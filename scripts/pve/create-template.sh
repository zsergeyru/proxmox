#!/usr/bin/env bash
set -Eeuo pipefail

# Канонический скрипт создания шаблона Debian 13 для Proxmox.
# Основной источник реализации и инфраструктурной конфигурации: https://github.com/zsergeyru/proxmox
# Публичный репозиторий начальной Stage 0: https://github.com/zsergeyru/proxmox-bootstrap
#
# Стандартный cloud-образ Debian запускается как VMID 9000, настраивается внутри
# гостевой системы через временный Cloud-Init user-data, перезагружается на обычное
# ядро Debian для проверки консоли и QEMU Guest Agent, очищается через QEMU Guest
# Agent и затем преобразуется в защищённый шаблон Proxmox.
#
# Модель доступа:
#   - у root нет рабочего пароля;
#   - аутентификация SSH по паролю отключена;
#   - root SSH разрешён только по публичному ключу;
#   - публичные SSH-ключи передаются каждому клону отдельно через Proxmox Cloud-Init;
#   - VGA/noVNC tty1 — основная консоль Proxmox с автоматическим входом под root;
#   - serial0 остаётся независимой резервной текстовой консолью с автовходом под root;
#   - наличие права Proxmox VM.Console фактически даёт административный root-доступ к гостю.

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
WAIT_PROGRESS_SECONDS="${WAIT_PROGRESS_SECONDS:-15}"
TEMPLATE_VERSION="${TEMPLATE_VERSION:-6}"

IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
CHECKSUM_URL="${CHECKSUM_URL:-https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS}"
IMAGE_NAME="${IMAGE_URL##*/}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/vz/template/cache/debian13}"
IMAGE_PATH="${IMAGE_DIR}/${IMAGE_NAME}"
CHECKSUM_PATH="${IMAGE_DIR}/SHA512SUMS"
SNIPPET_NAME="debian13-template-builder-${VMID}.yaml"
SNIPPET_VOL="${SNIPPET_STORAGE}:snippets/${SNIPPET_NAME}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОШИБКА: %s\n' "$*" >&2; exit 1; }

on_error() {
    local rc=$?
    printf '\nСоздание шаблона остановлено с кодом возврата %s.\n' "$rc" >&2
    printf 'Временная VM-сборщик и её диски намеренно оставлены для диагностики.\n' >&2
    printf 'Ничего автоматически не удаляется.\n' >&2
    exit "$rc"
}
trap on_error ERR

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Не найдена обязательная команда: $1"
}

fetch_file() {
    local url=$1
    local dst=$2

    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --retry 3 --output "$dst" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget --tries=3 --output-document="$dst" "$url"
    else
        die "На хосте Proxmox не установлен ни curl, ни wget"
    fi
}

vm_exists() {
    qm status "$VMID" >/dev/null 2>&1
}

wait_progress() {
    local phase=$1
    local started=$2
    local limit=$3
    local detail=${4:-}
    local elapsed=$((SECONDS - started))
    local vm_state
    vm_state="$(qm status "$VMID" 2>/dev/null | awk '{print $2}' || true)"
    [[ -n "$vm_state" ]] || vm_state="unknown"
    printf '[ОЖИДАНИЕ] %s: %ss/%ss, VM=%s%s\n' "$phase" "$elapsed" "$limit" "$vm_state" "$detail"
}

wait_for_agent() {
    local phase=${1:-QEMU Guest Agent}
    local started=$SECONDS
    local deadline=$((started + WAIT_SECONDS))
    local next_report=$started
    while (( SECONDS < deadline )); do
        if qm agent "$VMID" ping >/dev/null 2>&1; then
            printf '[ОК] %s доступен через %s с\n' "$phase" "$((SECONDS - started))"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            wait_progress "$phase" "$started" "$WAIT_SECONDS" ', agent=нет ответа'
            next_report=$((SECONDS + WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

wait_for_bootstrap() {
    local started=$SECONDS
    local deadline=$((started + WAIT_SECONDS))
    local next_report=$started
    local out
    while (( SECONDS < deadline )); do
        out="$(qm guest exec "$VMID" -- /bin/cat /var/lib/template-build/bootstrap-complete 2>/dev/null || true)"
        if grep -q 'BOOTSTRAP_OK' <<<"$out"; then
            printf '[ОК] Начальная настройка гостя завершена через %s с\n' "$((SECONDS - started))"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            wait_progress "Начальная настройка гостя" "$started" "$WAIT_SECONDS" ', QGA=ok; выполняются apt/full-upgrade/install'
            next_report=$((SECONDS + WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

wait_for_cloud_init() {
    local started=$SECONDS
    local deadline=$((started + WAIT_SECONDS))
    local next_report=$started
    local out
    while (( SECONDS < deadline )); do
        out="$(qm guest exec "$VMID" -- /bin/bash -lc 'cloud-init status' 2>/dev/null || true)"
        if grep -q 'status: done' <<<"$out"; then
            printf '[ОК] Cloud-Init завершён через %s с\n' "$((SECONDS - started))"
            return 0
        fi
        if grep -q 'status: error' <<<"$out"; then
            printf '%s\n' "$out" >&2
            return 1
        fi
        if (( SECONDS >= next_report )); then
            wait_progress "Cloud-Init" "$started" "$WAIT_SECONDS" ', QGA=ok; status ещё не done'
            next_report=$((SECONDS + WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

wait_for_new_boot_id() {
    local previous=$1
    local started=$SECONDS
    local deadline=$((started + WAIT_SECONDS))
    local next_report=$started
    local current
    while (( SECONDS < deadline )); do
        current="$(qm guest exec "$VMID" -- /bin/cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
        if [[ -n "$current" && "$current" != "$previous" ]]; then
            printf '[ОК] Проверочная перезагрузка завершена через %s с\n' "$((SECONDS - started))"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            wait_progress "Проверочная перезагрузка" "$started" "$WAIT_SECONDS" ', ожидается новый boot_id/QGA'
            next_report=$((SECONDS + WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

wait_for_stopped() {
    local limit=300
    local started=$SECONDS
    local deadline=$((started + limit))
    local next_report=$started
    while (( SECONDS < deadline )); do
        if [[ "$(qm status "$VMID" | awk '{print $2}')" == "stopped" ]]; then
            printf '[ОК] VM-сборщик выключена через %s с\n' "$((SECONDS - started))"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            wait_progress "Выключение VM-сборщика" "$started" "$limit"
            next_report=$((SECONDS + WAIT_PROGRESS_SECONDS))
        fi
        sleep 3
    done
    return 1
}

[[ $EUID -eq 0 ]] || die "Запустите этот скрипт от root на хосте Proxmox"
for cmd in qm pvesm sha512sum awk grep sed; do
    require_cmd "$cmd"
done

[[ "$VMID" =~ ^[0-9]+$ ]] || die "VMID должен быть числом"
[[ "$TEMPLATE_VERSION" =~ ^[0-9]+$ ]] || die "TEMPLATE_VERSION должен быть числом"
[[ "$WAIT_SECONDS" =~ ^[0-9]+$ ]] || die "WAIT_SECONDS должен быть числом"
[[ "$WAIT_PROGRESS_SECONDS" =~ ^[0-9]+$ ]] || die "WAIT_PROGRESS_SECONDS должен быть числом"
(( WAIT_PROGRESS_SECONDS >= 5 )) || die "WAIT_PROGRESS_SECONDS должен быть не меньше 5 секунд"

vm_exists && die "VMID ${VMID} уже существует. Скрипт не будет перезаписывать или удалять его."
pvesm status --storage "$DISK_STORAGE" >/dev/null 2>&1 || die "Хранилище '${DISK_STORAGE}' недоступно"
pvesm status --storage "$SNIPPET_STORAGE" >/dev/null 2>&1 || die "Хранилище snippets '${SNIPPET_STORAGE}' недоступно"

if ! SNIPPET_PATH="$(pvesm path "$SNIPPET_VOL" 2>/dev/null)"; then
    die "Для хранилища '${SNIPPET_STORAGE}' не включён тип содержимого Snippets. Включите 'Snippets' в настройках хранилища Proxmox и повторите запуск."
fi

[[ ! -e "$SNIPPET_PATH" ]] || die "Временный snippet уже существует: ${SNIPPET_PATH}"
mkdir -p "$(dirname "$SNIPPET_PATH")" "$IMAGE_DIR"

log "Загрузка стандартного cloud-образа Debian 13"
fetch_file "$IMAGE_URL" "$IMAGE_PATH"
fetch_file "$CHECKSUM_URL" "$CHECKSUM_PATH"

log "Проверка контрольной суммы SHA-512"
checksum_line="$(awk -v f="$IMAGE_NAME" '$2 == f || $2 == ("*" f) {print; exit}' "$CHECKSUM_PATH")"
[[ -n "$checksum_line" ]] || die "Для ${IMAGE_NAME} не найдена контрольная сумма в ${CHECKSUM_URL}"
IMAGE_SHA512="$(awk '{print $1}' <<<"$checksum_line")"
[[ "$IMAGE_SHA512" =~ ^[0-9a-fA-F]{128}$ ]] || die "Некорректная контрольная сумма SHA-512 для ${IMAGE_NAME}"
(
    cd "$IMAGE_DIR"
    printf '%s\n' "$checksum_line" | sha512sum --check --strict -
)

log "Создание временной конфигурации Cloud-Init для сборки"
cat >"$SNIPPET_PATH" <<'CLOUDCFG'
#cloud-config
hostname: builder-debian13
manage_etc_hosts: true
timezone: Europe/Moscow
ssh_pwauth: false
disable_root: false

# В проекте используется обычный SSH по сети. Автоматические SSH-сокеты
# systemd через AF_VSOCK/AF_UNIX не нужны и отключаются максимально рано.
bootcmd:
  - [mkdir, -p, /etc/systemd/system-generators]
  - [ln, -sfn, /dev/null, /etc/systemd/system-generators/systemd-ssh-generator]

users:
  - default

write_files:
  - path: /etc/ssh/sshd_config.d/00-template-security.conf
    owner: root:root
    permissions: '0644'
    content: |
      PermitRootLogin prohibit-password
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
      ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM

  - path: /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
    owner: root:root
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,57600,38400,9600 %I $TERM

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
      Управляемая виртуальная машина
      Базовый шаблон: tpl-debian13
      Инфраструктура: zsergeyru/proxmox
      Не храните секреты в Git.

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
        printf 'Пакет cloud-ядра всё ещё установлен после удаления\n' >&2
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
      sshd_effective="$(/usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1)"
      grep -Eq '^permitrootlogin (prohibit-password|without-password)$' <<<"$sshd_effective"
      grep -q '^passwordauthentication no$' <<<"$sshd_effective"
      grep -q '^kbdinteractiveauthentication no$' <<<"$sshd_effective"
      grep -q '^permitemptypasswords no$' <<<"$sshd_effective"
      grep -q '^pubkeyauthentication yes$' <<<"$sshd_effective"
      passwd -S root | grep -q ' L '

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
      rm -f /root/.bash_history
      rm -rf /root/.ssh
      rm -rf /var/lib/template-build
      rm -f /usr/local/sbin/template-bootstrap /usr/local/sbin/template-finalize

      fstrim -av || printf 'ПРЕДУПРЕЖДЕНИЕ: fstrim завершился с ошибкой; финализация шаблона продолжается\n' >&2
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

log "Создание временной VM-сборщика ${VMID}"
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

log "Импорт системного диска"
qm importdisk "$VMID" "$IMAGE_PATH" "$DISK_STORAGE"
IMPORTED_DISK="$(qm config "$VMID" | awk -F': ' '/^unused[0-9]+:/ {print $2; exit}')"
[[ -n "$IMPORTED_DISK" ]] || die "Импортированный диск не найден в конфигурации VM"

qm set "$VMID" --scsi0 "${IMPORTED_DISK},discard=on,iothread=1,ssd=1"
qm resize "$VMID" scsi0 "$DISK_SIZE"
qm set "$VMID" --ide2 "${DISK_STORAGE}:cloudinit"
qm set "$VMID" --boot "order=scsi0"
qm set "$VMID" --ipconfig0 ip=dhcp
qm set "$VMID" --cicustom "user=${SNIPPET_VOL}"

log "Запуск временной VM-сборщика"
qm start "$VMID"

log "Ожидание первого QEMU Guest Agent (Cloud-Init выполняет apt update/full-upgrade и устанавливает agent)"
wait_for_agent "QEMU Guest Agent" || die "QEMU Guest Agent не стал доступен за ${WAIT_SECONDS} секунд"

log "Ожидание завершения начальной настройки гостя"
wait_for_bootstrap || die "Начальная настройка гостя не завершилась за ${WAIT_SECONDS} секунд. Проверьте консоль VM ${VMID} и журналы cloud-init."

log "Ожидание финальной стадии Cloud-Init"
wait_for_cloud_init || die "Cloud-Init не завершился корректно со статусом done"

log "Перезагрузка VM-сборщика на обычное ядро Debian"
BOOT_ID_BEFORE="$(qm guest exec "$VMID" -- /bin/cat /proc/sys/kernel/random/boot_id)"
qm reboot "$VMID"
wait_for_new_boot_id "$BOOT_ID_BEFORE" || die "VM-сборщик не завершила проверочную перезагрузку за ${WAIT_SECONDS} секунд"
wait_for_agent "QEMU Guest Agent после перезагрузки" || die "QEMU Guest Agent не подключился повторно после проверочной перезагрузки"

log "Проверка ядра, framebuffer и консольных служб после перезагрузки"
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
grep -q -- "--autologin root" /etc/systemd/system/getty@tty1.service.d/autologin.conf
grep -q -- "--autologin root" /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
printf "VERIFY_KERNEL=%s VERIFY_FRAMEBUFFER=%s CONSOLES_OK\n" "$kernel" "$framebuffer"
')"
grep -q 'CONSOLES_OK' <<<"$VERIFY_OUTPUT" || die "Проверка консолей после перезагрузки не сообщила об успешном завершении"
KERNEL_TOKEN="$(grep -oE 'VERIFY_KERNEL=[^[:space:]\"]+' <<<"$VERIFY_OUTPUT" | head -1)"
FRAMEBUFFER_TOKEN="$(grep -oE 'VERIFY_FRAMEBUFFER=[0-9]+,[0-9]+' <<<"$VERIFY_OUTPUT" | head -1)"
KERNEL_VERSION="${KERNEL_TOKEN#*=}"
FRAMEBUFFER_SIZE="${FRAMEBUFFER_TOKEN#*=}"
[[ -n "$KERNEL_VERSION" ]] || die "Проверенная версия ядра не была получена"
[[ -n "$FRAMEBUFFER_SIZE" ]] || die "Проверенный размер framebuffer не был получен"

log "Финальная очистка внутри гостевой системы"
FINALIZE_OUTPUT="$(qm guest exec "$VMID" -- /usr/local/sbin/template-finalize)"
grep -q 'FINALIZE_OK' <<<"$FINALIZE_OUTPUT" || die "Финализация гостя не сообщила об успешном завершении"

log "Выключение VM-сборщика"
qm shutdown "$VMID" --timeout 180 || true
wait_for_stopped || die "VM-сборщик не выключилась корректно"

log "Удаление временного Cloud-Init сборщика и настройка параметров клона"
qm set "$VMID" --delete cicustom
qm set "$VMID" --ciuser root
qm set "$VMID" --ciupgrade 0
qm set "$VMID" --ipconfig0 ip=dhcp
qm set "$VMID" --name "$TEMPLATE_NAME"
qm set "$VMID" --description "Базовый шаблон Debian 13 (Trixie); template-version=${TEMPLATE_VERSION}; root SSH key-only; VGA/noVNC tty1 с автовходом; резервный serial0; SSH-ключи передаются каждому клону отдельно"

log "Пересоздание стандартного Cloud-Init диска Proxmox"
qm cloudinit update "$VMID"
CLOUDINIT_USER_DATA="$(qm cloudinit dump "$VMID" user)"
if grep -qE 'template-bootstrap|builder-debian13|/usr/local/sbin/template-finalize' <<<"$CLOUDINIT_USER_DATA"; then
    die "После пересоздания Cloud-Init всё ещё содержит временные данные сборщика"
fi

rm -f "$SNIPPET_PATH"

log "Преобразование VM ${VMID} в шаблон"
qm template "$VMID"

log "Защита базового шаблона от случайного удаления"
qm set "$VMID" --protection 1

FINAL_CONFIG="$(qm config "$VMID")"
grep -q '^template: 1$' <<<"$FINAL_CONFIG" || die "VM ${VMID} не была отмечена как шаблон"
grep -q '^protection: 1$' <<<"$FINAL_CONFIG" || die "Защита шаблона не была включена"
grep -q '^agent: 1$' <<<"$FINAL_CONFIG" || die "Поддержка QEMU Guest Agent не включена в конфигурации шаблона"
grep -q '^vga: std$' <<<"$FINAL_CONFIG" || die "Для шаблона не установлен VGA-дисплей std"
grep -q '^serial0: socket$' <<<"$FINAL_CONFIG" || die "serial0 шаблона не настроен как socket"
grep -q '^ciuser: root$' <<<"$FINAL_CONFIG" || die "Пользователь Cloud-Init шаблона должен быть root"
grep -q '^ciupgrade: 0$' <<<"$FINAL_CONFIG" || die "Автоматическое обновление пакетов Cloud-Init не отключено"
grep -q '^ipconfig0: ip=dhcp$' <<<"$FINAL_CONFIG" || die "Сеть шаблона по умолчанию должна использовать DHCP"
grep -q "template-version=${TEMPLATE_VERSION}" <<<"$FINAL_CONFIG" || die "Description шаблона не содержит ожидаемую версию ${TEMPLATE_VERSION}"
if grep -q '^cicustom:' <<<"$FINAL_CONFIG"; then
    die "В конфигурации шаблона остался временный cicustom сборщика"
fi

trap - ERR

cat <<EOF

Шаблон успешно создан.

VMID:              ${VMID}
Имя:               ${TEMPLATE_NAME}
Версия:            ${TEMPLATE_VERSION}
Хранилище:         ${DISK_STORAGE}
Сеть:              DHCP по умолчанию
Management user:   root
Root password:     заблокирован
Root SSH:          только по публичному ключу
Консоль:           Proxmox noVNC / VGA tty1, автовход под root
Serial:            serial0 / ttyS0, автовход под root (резервная консоль)
Ядро:              ${KERNEL_VERSION}
Framebuffer:       ${FRAMEBUFFER_SIZE}
SSH:               передавайте один или несколько публичных ключей каждому клону через Cloud-Init
Обновления:        автоматическое обновление пакетов Cloud-Init отключено (ciupgrade=0)
Защита:            для базового шаблона включена Proxmox protection
Образ:             ${IMAGE_NAME}
SHA-512:           ${IMAGE_SHA512}

Рекомендуемый следующий шаг: создать FULL-клон, до первого запуска задать ему
публичные SSH-ключи и сетевые параметры, пересоздать Cloud-Init диск, затем проверить
автовход noVNC/tty1, резервную консоль serial0, QEMU Guest Agent, root-доступ по SSH,
уникальные machine-id и host keys, обычное ядро amd64, наличие framebuffer и
расширение файловой системы.

EOF