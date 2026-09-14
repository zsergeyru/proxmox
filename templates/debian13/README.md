# Базовый шаблон Debian 13 для Proxmox

Статус: **скрипт сборки реализован и проверен полной чистой сборкой на реальном PVE-хосте 2026-09-14**.

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 2
```

Рабочие VM по умолчанию создаются как **Full Clone**.

## Источники истины

- [`build-policy.md`](./build-policy.md) — политика сборки и базовых настроек;
- [`users-and-keys.md`](./users-and-keys.md) — пользователи, SSH-ключи и доступ через Proxmox console;
- [`filesystem-layout.md`](./filesystem-layout.md) — файловая структура сервисов;
- [`create-template.sh`](./create-template.sh) — рабочая инфраструктурная копия автоматизированной сборки;
- [`../../docs/bootstrap.md`](../../docs/bootstrap.md) — публичный bootstrap-репозиторий и способ запуска непосредственно на PVE.

Публичная утверждённая копия скрипта распространяется через `zsergeyru/proxmox-bootstrap`. Она позволяет PVE скачать один файл по HTTPS без Git и GitHub PAT.

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
| Proxmox display | `vga: std` |
| Main console | noVNC → `tty1` → autologin `ops` |
| Serial console | `serial0: socket`, без autologin |
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

```text
Proxmox Web UI
→ VM → Console
→ noVNC
→ tty1
→ autologin ops
→ shell
```

Это не пустой пароль. Пароль `ops` остаётся заблокированным. Autologin разрешён только на `tty1`.

Право открыть console VM в Proxmox считается административным доступом к гостевой ОС: `ops` имеет `NOPASSWD: sudo`.

`serial0` остаётся отдельным резервным диагностическим каналом без autologin.

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
Template-Version: 2
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

Штатный способ — скачать утверждённую публичную копию bootstrap-скрипта. Git и GitHub token на PVE для этого не нужны:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/create-template.sh \
  -o /root/create-template.sh

bash -n /root/create-template.sh
chmod +x /root/create-template.sh
/root/create-template.sh
```

Приватная копия `templates/debian13/create-template.sh` используется как инфраструктурный source of truth и должна синхронизироваться с публичной утверждённой копией перед запуском.

По умолчанию:

```text
VMID=9000
DISK_STORAGE=local-lvm
SNIPPET_STORAGE=local
BRIDGE=vmbr0
DISK_SIZE=16G
WAIT_SECONDS=1200
TEMPLATE_VERSION=2
```

## Проверенный clean build v2

2026-09-14 выполнена полная повторная сборка с нуля на реальном PVE-хосте после устранения ошибок первого прогона. Успешно пройдены:

1. загрузка Debian 13 genericcloud image;
2. SHA-512 verification;
3. импорт 3 GiB cloud image в `local-lvm` и resize системного диска до 16 GiB;
4. временный Cloud-Init bootstrap;
5. установка пакетов и настройка locale/timezone;
6. запуск QEMU Guest Agent;
7. успешное завершение `Cloud-Init final stage`;
8. `template-finalize`, очистка machine-specific данных и shutdown;
9. удаление builder-only `cicustom` и регенерация стандартного Cloud-Init drive;
10. `qm template`;
11. включение `protection=1`;
12. финальное сообщение `Template created successfully.`.

Первый прогон выявил две ошибки порядка Cloud-Init: раннюю настройку `locale` и слишком раннее удаление default-user `debian`. Обе исправлены до успешного clean build.

## Проверка после сборки

После успешного clean build остаётся проверить поведение **клона**. Создать тестовый Full Clone, задать SSH public key и network **до первого старта**, затем проверить:

1. VM загружается и получает сеть;
2. `VM → Console` открывает noVNC и автоматически даёт shell `ops`;
3. пароль `ops` остаётся locked;
4. SSH доступен только по ключу;
5. `sudo` работает без пароля;
6. QEMU Guest Agent отвечает;
7. `serial0` работает без autologin;
8. machine-id уникален;
9. SSH host keys уникальны;
10. root filesystem увеличивается после resize диска;
11. `/etc/vm-template-info` содержит правильную версию и SHA-512 исходного образа;
12. в конфигурации template есть `ciupgrade: 0`, `protection: 1`, `vga: std`, `serial0: socket`.
