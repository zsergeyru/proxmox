# Базовый шаблон Debian 13 для Proxmox

Статус: **Template-Version 4 реализована в каноническом builder-скрипте. GitHub CI проверяет внешний Bash, встроенный Cloud-Init YAML и оба guest-скрипта. Изменения v4 подтверждены вручную на тестовой VM: обычное `amd64` ядро создаёт framebuffer, VGA/noVNC console работает с нормальным размером шрифта, `tty1` autologin `ops` работает. Требуется чистая сборка VMID 9000 и финальная проверка нового Full Clone.**

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 4
```

Рабочие VM по умолчанию создаются как **Full Clone**.

## Источники истины

- [`build-policy.md`](./build-policy.md) — политика сборки и базовых настроек;
- [`users-and-keys.md`](./users-and-keys.md) — пользователи, SSH-ключи и console access;
- [`filesystem-layout.md`](./filesystem-layout.md) — файловая структура сервисов;
- [`../../docs/bootstrap.md`](../../docs/bootstrap.md) — расположение и запуск bootstrap-скрипта.

Единственный канонический экземпляр `create-template.sh` хранится в публичном `zsergeyru/proxmox-bootstrap`. В приватном `proxmox` executable-копия не хранится.

## Что делает v4

```text
официальный Debian 13 genericcloud image
        ↓
SHA-512 verification
        ↓
VMID 9000 builder-debian13
        ↓
временный Cloud-Init bootstrap
        ↓
apt update + full-upgrade
базовые пакеты
linux-image-amd64
console-setup + console-setup-linux
        ↓
удаление cloud-amd64 kernel
console font Fixed 8x16
        ↓
vga: std
VGA/noVNC → tty1 → autologin ops
serial0: socket → ttyS0 → autologin ops (fallback)
        ↓
ожидание QEMU Guest Agent и Cloud-Init
        ↓
verification reboot
        ↓
проверка:
обычный *-amd64 kernel
не cloud kernel
fb0 существует и сообщает непустое WIDTH,HEIGHT
QEMU Guest Agent
getty@tty1
serial-getty@ttyS0
SSH effective policy
locked ops/root passwords
        ↓
final cleanup
machine-id/SSH host keys/cloud-init state/logs и прочее
        ↓
shutdown
        ↓
стандартный Cloud-Init: ciuser=ops, DHCP, ciupgrade=0
        ↓
qm template
protection=1
```

При ошибке builder-VM автоматически не удаляется. Существующий VMID `9000` скрипт не перезаписывает.

## Почему обычное ядро, а не cloud kernel

В исходном `genericcloud` image Debian используется `cloud-amd64` kernel. На нашей VM с `vga: std` он не создавал framebuffer (`fb0` отсутствовал), поэтому noVNC работал в старом VGA text mode с крупными символами.

На тестовой VM после установки и загрузки обычного ядра:

```text
uname -r → 6.12.107+deb13-amd64
/sys/class/graphics/fb0/virtual_size → 1280,800
```

и Proxmox Console стала визуально такой же, как обычная Debian VM с мелким консольным шрифтом. Поэтому v4 устанавливает `linux-image-amd64`, удаляет cloud-kernel и проверяет результат реальной перезагрузкой builder.

Скрипт не требует именно `1280x800`: он проверяет наличие валидного framebuffer и выводит реально обнаруженное разрешение в итоговом отчёте. На текущем PVE фактическое значение — `1280x800`.

## Базовые параметры

| Параметр | Значение |
|---|---|
| CPU | 1 vCPU, type `host` |
| RAM | 1 GiB |
| System disk | 16 GiB |
| Storage | `local-lvm` |
| Controller | VirtIO SCSI Single |
| I/O thread | enabled |
| Discard/TRIM | enabled |
| SSD emulation | enabled |
| Network | VirtIO, `vmbr0` |
| Template network | DHCP |
| Cloud-Init | да |
| Cloud-Init package upgrade | `ciupgrade=0` |
| QEMU Guest Agent | да |
| Kernel | `linux-image-amd64`, cloud kernel удалён |
| Proxmox display | `vga: std` |
| Main console | noVNC/VGA → `tty1` → autologin `ops` |
| Console font | Fixed 8x16 |
| Framebuffer | обязателен; текущий PVE даёт 1280x800 |
| Serial fallback | `serial0: socket` → `ttyS0` → autologin `ops` |
| Admin user | `ops` |
| `ops` password | locked |
| `root` password | locked |
| Root SSH | запрещён |
| Password SSH | запрещён |
| SSH public key | задаётся клону до первого старта |
| Timezone | `Europe/Moscow` |
| Locale | `en_US.UTF-8` |
| Swap | не создаётся |
| Template protection | `1` |
| Default clone protection | `0` |

## Модель доступа

Сетевой доступ:

```text
SSH → ops + private key
```

SSH hardening:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Builder проверяет не только `sshd -t`, но и эффективные значения через `sshd -T`.

Основная локальная console:

```text
Proxmox Web UI
→ VM → Console
→ noVNC / VGA
→ tty1
→ automatic login ops
→ shell
```

Резервная console:

```text
xterm.js или qm terminal
→ serial0 / ttyS0
→ automatic login ops
→ shell
```

Autologin не разблокирует пароль `ops`. Право `VM.Console` считается административным доступом, поскольку `ops` имеет `NOPASSWD: sudo`.

## Cloud-Init конкретного клона

Base template не содержит персональных SSH-ключей. Для каждой VM параметры задаются **до первого запуска**:

```text
Full Clone
→ protection=0, если VM не должна быть защищена
→ name/hostname
→ ciuser=ops
→ sshkeys
→ IP/DHCP и DNS
→ qm cloudinit update
→ start VM
```

`cipassword` в штатном deploy-сценарии не используется.

Важно: template `9000` имеет `protection=1`; обычный deploy/AI-agent workflow после clone должен явно выставлять клону `protection=0`, если паспорт VM не требует защиты.

### Проверка Full Clone

При LVM-thin Linked Clone также может выглядеть как обычный диск в GUI. Надёжные проверки:

```text
Proxmox task log:
create full clone of drive scsi0 (...)
```

или на PVE:

```bash
lvs -o lv_name,origin
```

Для настоящего Full Clone поле `Origin` у `vm-<VMID>-disk-0` должно быть пустым. Если там `base-9000-disk-0`, это Linked Clone.

## Обновления

При сборке template выполняются `apt update` и `apt full-upgrade`. Для клонов автоматический package upgrade на первом запуске выключен (`ciupgrade=0`). Дальнейшие обновления выполняются контролируемо через deploy/Ansible/ручное администрирование.

## Disk growth и TRIM

В template установлен `cloud-guest-utils`, чтобы `growpart` был доступен при увеличении диска клона. Включён `fstrim.timer`, а перед финальным shutdown выполняется `fstrim -av`.

Ранее тестовый Full Clone подтвердил автоматическое расширение root partition/filesystem с 16 GiB до 24 GiB.

## Синхронизация времени

```text
systemd-timesyncd: enabled
Timezone: Europe/Moscow
Locale: en_US.UTF-8
```

## Информация о происхождении

В каждой VM сохраняется `/etc/vm-template-info`:

```text
Template: tpl-debian13
Template-Version: 4
OS: Debian 13
Kernel-Flavor: amd64
Primary-Console: VGA/noVNC tty1 autologin ops
Fallback-Console: serial0 ttyS0 autologin ops
Infrastructure-Source: zsergeyru/proxmox
Bootstrap-Source: zsergeyru/proxmox-bootstrap
Source-Image: debian-13-genericcloud-amd64.qcow2
Source-Image-SHA512: <128 hex chars>
Build-Date: YYYY-MM-DD
```

## Базовые пакеты

```text
qemu-guest-agent
openssh-server
sudo
locales
cloud-guest-utils
systemd-timesyncd
linux-image-amd64
console-setup
console-setup-linux

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
zstd
unzip
acl
dnsutils
iproute2
iputils-ping
net-tools
cron
logrotate
```

`wget` и `net-tools` намеренно остаются в base template: они небольшие и полезны при ручной диагностике. `restic` удалён из base template и должен устанавливаться только на VM, где реально нужен guest-level backup.

Docker и прикладные сервисы в base template не устанавливаются.

## Очистка перед template

Скрипт очищает:

- builder-only пользователя `debian` после завершения Cloud-Init;
- Cloud-Init state, logs и seed;
- `/etc/machine-id` и dbus machine-id;
- SSH host keys;
- DHCP/network state;
- systemd random seed;
- APT cache/lists;
- journal/build logs;
- temporary files;
- shell history;
- `/home/ops/.ssh`;
- builder scripts.

После удаления временного `cicustom` выполняется `qm cloudinit update` и проверяется отсутствие builder-only user-data.

## Финальные проверки builder

После `qm template` скрипт дополнительно проверяет:

```text
template: 1
protection: 1
agent: 1
vga: std
serial0: socket
ciuser: ops
ciupgrade: 0
ipconfig0: ip=dhcp
cicustom: отсутствует
```

## CI канонического скрипта

GitHub Actions публичного `proxmox-bootstrap` проверяет:

- синтаксис внешнего `create-template.sh`;
- встроенный Cloud-Init как YAML;
- синтаксис встроенного `template-bootstrap`;
- синтаксис встроенного `template-finalize`;
- whitespace errors.

Эта CI-проверка дополняет, но не заменяет clean build на реальном PVE.

## Защита template

После успешного `qm template`:

```text
9000 → protection=1
```

Обычные рабочие клоны:

```text
protection=0
```

Protection включается на конкретном госте только по явному решению.

## Запуск на Proxmox

```bash
curl -fsSL \
  https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/create-template.sh \
  -o /root/create-template.sh

bash -n /root/create-template.sh
chmod +x /root/create-template.sh
/root/create-template.sh
```

По умолчанию:

```text
VMID=9000
DISK_STORAGE=local-lvm
SNIPPET_STORAGE=local
BRIDGE=vmbr0
DISK_SIZE=16G
WAIT_SECONDS=1200
TEMPLATE_VERSION=4
```

## История

### v2

2026-09-14 успешно проверены clean build и тестовый Full Clone: locked `ops`, `sudo NOPASSWD`, SSH по ключу, QGA, timesync, fstrim, очистка builder artifacts, уникальные machine-id/SSH host keys и filesystem growth.

### v3

Проверялся вариант, где штатная Web Console была направлена на `serial0` через `vga: serial0`. Он работал технически, но serial-консоль ведёт себя как поток: не хранит старое содержимое экрана и менее удобна для обычной работы.

### v4

Возвращён `vga: std`. Эксперимент на тестовой VM показал, что крупный текст был вызван не noVNC, а `cloud-amd64` kernel без framebuffer. После загрузки обычного `amd64` kernel появился `fb0` `1280x800`, а console стала нормального размера. Также подтверждён `tty1` autologin `ops`.

## Финальная проверка v4

После чистой сборки создать новый **Full Clone** и проверить:

1. `VM → Console` открывает VGA/noVNC и автоматически даёт shell `ops` на `tty1`;
2. применяется `Fixed 8x16`, `fb0` существует и сообщает валидное разрешение; текущее ожидаемое на нашем PVE — `1280x800`;
3. `uname -r` не содержит `cloud`;
4. `serial0` остаётся рабочим резервным каналом;
5. пароли `ops` и `root` locked, `sudo` без пароля;
6. SSH работает только по public key и эффективная SSH policy соответствует требованиям;
7. QEMU Guest Agent отвечает сразу после boot и после reboot;
8. machine-id и SSH host keys уникальны;
9. filesystem увеличивается после resize;
10. builder artifacts отсутствуют;
11. `/etc/vm-template-info` содержит `Template-Version: 4` и правильный SHA-512;
12. template имеет `protection=1`, клон — `protection=0`;
13. `lvs ... origin` подтверждает Full Clone;
14. config template содержит `agent: 1`, `vga: std`, `serial0: socket`, `ciuser: ops`, `ciupgrade: 0`, `ipconfig0: ip=dhcp` и не содержит `cicustom`.
