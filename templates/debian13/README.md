# Базовый шаблон Debian 13 для Proxmox

Статус: **Template-Version 3 реализована в builder-скрипте; предыдущая v2 успешно проверена полной чистой сборкой на реальном PVE-хосте 2026-09-14. Для v3 требуется новая чистая сборка и проверка клона.**

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 3
```

Рабочие VM по умолчанию создаются как **Full Clone**.

## Источники истины

- [`build-policy.md`](./build-policy.md) — политика сборки и базовых настроек;
- [`users-and-keys.md`](./users-and-keys.md) — пользователи, SSH-ключи и доступ через Proxmox console;
- [`filesystem-layout.md`](./filesystem-layout.md) — файловая структура сервисов;
- [`../../docs/bootstrap.md`](../../docs/bootstrap.md) — расположение и способ запуска bootstrap-скрипта непосредственно на PVE.

Единственный канонический экземпляр `create-template.sh` хранится в публичном репозитории `zsergeyru/proxmox-bootstrap`. В приватном `proxmox` executable-копия не хранится.

## Что делает скрипт

```text
официальный Debian 13 genericcloud image
        ↓
проверка SHA-512 по официальному SHA512SUMS
        ↓
VMID 9000 builder-debian13
        ↓
временный Cloud-Init bootstrap
        ↓
apt update + full-upgrade
базовые пакеты и системные настройки
        ↓
serial0 + xterm.js как основная текстовая консоль
serial-getty@ttyS0 → autologin ops
        ↓
ожидание QEMU Guest Agent
ожидание окончания Cloud-Init
        ↓
финальная очистка и удаление builder-only пользователя debian
очистка machine-specific данных
fstrim
shutdown
        ↓
удаление builder-only Cloud-Init
стандартный Cloud-Init: ciuser=ops, DHCP, ciupgrade=0
        ↓
регенерация Cloud-Init drive и проверка
        ↓
qm template
        ↓
protection=1
```

При ошибке builder-VM автоматически не удаляется. Существующий VMID `9000` скрипт не перезаписывает.

## Базовые параметры

| Параметр | Значение |
|---|---|
| CPU | 1 vCPU, type `host` |
| RAM | 1 GiB |
| System disk | 16 GiB |
| Storage | `local-lvm` по умолчанию |
| Controller | VirtIO SCSI Single |
| I/O thread | enabled |
| Discard/TRIM | enabled |
| SSD emulation | enabled |
| Network | VirtIO, `vmbr0` |
| Template network | DHCP |
| Cloud-Init | да |
| Cloud-Init package upgrade | выключен (`ciupgrade=0`) |
| QEMU Guest Agent | да |
| Proxmox display | `vga: serial0` |
| Main console | xterm.js → `serial0` → autologin `ops` |
| Serial device | `serial0: socket` |
| Admin user | `ops` |
| `ops` password | locked |
| Root SSH | запрещён |
| Password SSH | запрещён |
| SSH public key | задаётся каждому клону до первого старта |
| Timezone | `Europe/Moscow` |
| Locale | `en_US.UTF-8` |
| Swap | не создаётся |
| Template protection | включена |

## Модель доступа

Штатный сетевой доступ:

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

Файл настроек:

```text
/etc/ssh/sshd_config.d/00-template-security.conf
```

### Proxmox Console

Основная console теперь текстовая:

```text
Proxmox Web UI
→ VM → Console
→ xterm.js
→ serial0 / ttyS0
→ autologin ops
→ shell
```

Это не пустой пароль. Пароль `ops` остаётся заблокированным. Autologin разрешён локально на `serial0` через `serial-getty@ttyS0`.

Право открыть console VM в Proxmox считается административным доступом к гостевой ОС: `ops` имеет `NOPASSWD: sudo`.

Файл override:

```text
/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
```

`tty1` больше не является основной административной консолью и специальный autologin на нём не настраивается.

## Cloud-Init конкретного клона

Base template не содержит персональных SSH-ключей. Для каждой VM ключ и сеть задаются **до первого запуска**.

Правильная последовательность развёртывания:

```text
Full Clone
→ задать name/hostname
→ ciuser=ops
→ задать sshkeys
→ задать IP/DHCP и DNS
→ qm cloudinit update
→ start VM
```

`cipassword` в штатном deploy-сценарии не используется.

## Обновления

При сборке template выполняются:

```text
apt update
apt full-upgrade
```

Для клонов автоматический package upgrade на первом запуске выключен:

```text
ciupgrade=0
```

Дальнейшие обновления VM выполняются контролируемо через deploy/Ansible/ручное администрирование.

## Disk growth и TRIM

В template установлен `cloud-guest-utils`, чтобы `growpart` был доступен при увеличении диска клона.

Включён `fstrim.timer`, а перед финальным shutdown выполняется `fstrim -av`.

## Синхронизация времени

Установлен и включён `systemd-timesyncd`.

```text
Timezone: Europe/Moscow
Locale: en_US.UTF-8
```

## Информация о происхождении

В каждой VM сохраняется:

```text
/etc/vm-template-info
```

Пример:

```text
Template: tpl-debian13
Template-Version: 3
OS: Debian 13
Infrastructure-Source: zsergeyru/proxmox
Bootstrap-Source: zsergeyru/proxmox-bootstrap
Source-Image: debian-13-genericcloud-amd64.qcow2
Source-Image-SHA512: <128 hex chars>
Build-Date: YYYY-MM-DD
```

`TEMPLATE_VERSION` подставляется в этот файл из параметра build-скрипта.

## Базовые пакеты

```text
qemu-guest-agent
openssh-server
sudo
locales
cloud-guest-utils
systemd-timesyncd

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
```

Docker и прикладные сервисы в base template не устанавливаются.

## Очистка перед template

Скрипт очищает:

- builder-only пользователя `debian` после успешного завершения Cloud-Init;
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
- builder scripts `template-bootstrap` и `template-finalize`.

После удаления временного `cicustom` скрипт выполняет `qm cloudinit update` и проверяет, что builder-only user-data больше не присутствует.

## Защита template

После успешного `qm template` включается:

```text
protection=1
```

Это защита от случайного удаления, а не отдельная security boundary.

Для осознанной пересборки VMID `9000` сначала нужно снять protection и удалить старый template вручную.

## Требования перед запуском

На Proxmox должны быть доступны:

```text
qm
pvesm
sha512sum
awk
grep
sed
curl или wget
```

Storage `local` должен поддерживать content type **Snippets**:

```text
Datacenter → Storage → local → Edit → Content → Snippets
```

VMID `9000` должен быть свободен.

## Запуск на Proxmox

Штатный способ — скачать канонический bootstrap-скрипт из публичного `zsergeyru/proxmox-bootstrap`. Git и GitHub token на PVE для этого не нужны:

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
TEMPLATE_VERSION=3
```

## История проверки

### Проверенный clean build v2

2026-09-14 выполнена полная повторная сборка v2 с нуля на реальном PVE-хосте после устранения ошибок первого прогона. Успешно пройдены загрузка и SHA-512 verification, импорт и resize диска, Cloud-Init bootstrap, QEMU Guest Agent, `Cloud-Init final stage`, cleanup, shutdown, регенерация Cloud-Init drive, `qm template` и `protection=1`.

Первый прогон выявил две ошибки порядка Cloud-Init: раннюю настройку `locale` и слишком раннее удаление default-user `debian`. Обе исправлены до успешного clean build v2.

### Изменение v3

В v3 основная консоль изменена с framebuffer/noVNC/tty1 на настоящую текстовую serial-консоль:

```text
vga: serial0
serial0: socket
xterm.js → ttyS0 → autologin ops
```

После изменения требуется новая чистая сборка v3 и повторная проверка тестового Full Clone.

## Проверка после сборки v3

Создать тестовый Full Clone, задать SSH public key и network **до первого старта**, затем проверить:

1. VM загружается и получает сеть;
2. `VM → Console` открывает текстовую xterm.js-консоль через `serial0`;
3. xterm.js автоматически даёт shell `ops`;
4. пароль `ops` остаётся locked;
5. SSH доступен только по ключу;
6. `sudo` работает без пароля;
7. QEMU Guest Agent отвечает;
8. machine-id уникален;
9. SSH host keys уникальны;
10. root filesystem увеличивается после resize диска;
11. `/etc/vm-template-info` содержит правильную версию и SHA-512 исходного образа;
12. в конфигурации template есть `ciupgrade: 0`, `protection: 1`, `vga: serial0`, `serial0: socket`.
