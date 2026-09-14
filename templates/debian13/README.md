# Базовый шаблон Debian 13 для Proxmox

Статус: **скрипт сборки реализован**.

Базовый template — универсальная основа для Linux VM в Proxmox:

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 2
```

Рабочие VM по умолчанию создаются как **Full Clone**.

## Источники истины

- [`build-policy.md`](./build-policy.md) — политика сборки и базовых настроек;
- [`users-and-keys.md`](./users-and-keys.md) — пользователи, console access и SSH-ключи;
- [`filesystem-layout.md`](./filesystem-layout.md) — файловая структура сервисов;
- [`create-template.sh`](./create-template.sh) — автоматизированная сборка.

## Что делает скрипт

Сборка выполняется без `virt-customize` и без установки `libguestfs-tools` на Proxmox:

```text
официальный Debian 13 genericcloud image
        ↓
VMID 9000 builder-debian13
        ↓
временный Cloud-Init bootstrap
        ↓
apt update + full-upgrade
базовые пакеты и настройки
        ↓
проверка через QEMU Guest Agent
        ↓
очистка machine-specific данных
        ↓
shutdown
        ↓
удаление builder-only Cloud-Init
        ↓
стандартный Cloud-Init для клонов
ciuser=ops + DHCP
        ↓
qm template
        ↓
tpl-debian13
```

Template **не содержит** личного SSH public key и **не содержит рабочего пароля `ops`**. Оба значения задаются конкретной VM через Cloud-Init после клонирования.

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
| QEMU Guest Agent | да |
| Serial console | да |
| Admin user | `ops` |
| Root SSH | запрещён |
| Password SSH | запрещён |
| Console password | задаётся каждому клону через Cloud-Init |
| SSH public key | задаётся каждому клону через Cloud-Init |
| Timezone | `Europe/Moscow` |
| Locale | `en_US.UTF-8` |
| Swap | не создаётся |
| Template-Version | `2` |

## Доступ к клонам

После создания Full Clone для конкретной VM через Cloud-Init задаются:

```text
User: ops
Password: пароль для локальной/Proxmox console
SSH public key: ключ нужного административного устройства
Network: DHCP или статический IP
```

Пароль нужен для console login, но SSH по паролю остаётся запрещённым:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

То есть итоговая модель:

```text
Proxmox serial console
→ ops + password конкретной VM

SSH
→ ops + private key на компьютере
→ public key передан VM через Cloud-Init
```

Сам template остаётся без персональных credentials.

## Требования перед запуском

На Proxmox должны быть доступны:

```text
qm
pvesm
sha512sum
curl или wget
```

Storage `local` должен поддерживать content type **Snippets**:

```text
Datacenter → Storage → local → Edit → Content → Snippets
```

VMID `9000` должен быть свободен.

## Запуск

```bash
cd /path/to/proxmox/templates/debian13
chmod +x create-template.sh
sudo ./create-template.sh
```

По умолчанию:

```text
VMID=9000
DISK_STORAGE=local-lvm
SNIPPET_STORAGE=local
BRIDGE=vmbr0
DISK_SIZE=16G
```

При необходимости значения переопределяются переменными окружения.

## Базовые пакеты

```text
qemu-guest-agent
openssh-server
sudo
locales

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

## Пользователь `ops`

В template создаётся `ops` с группами:

```text
adm
sudo
```

В самом template пароль `ops` заблокирован. Cloud-Init конкретного клона задаёт рабочий пароль и SSH public keys.

`root` как системная учётная запись Debian сохраняется, но прямой SSH-login root запрещён.

## Очистка перед template

Перед `qm template` очищаются:

- Cloud-Init state;
- machine-id;
- SSH host keys;
- DHCP lease/state;
- random seed;
- APT cache/lists;
- временные файлы;
- journal/build logs;
- shell history;
- `.ssh` пользователя `ops`.

Каждый клон формирует собственные machine-id и SSH host keys.

## Проверка после сборки

Создать тестовый **Full Clone**, задать ему через Cloud-Init пароль и SSH public key, затем проверить:

1. загрузку VM;
2. получение сети;
3. новый machine-id;
4. уникальные SSH host keys;
5. вход `ops` через Proxmox console по паролю;
6. вход `ops` по SSH-ключу;
7. `sudo`;
8. QEMU Guest Agent;
9. serial console;
10. размер root filesystem;
11. `/etc/vm-template-info`.

## Что не входит в base template

Не устанавливаются заранее Docker/Compose, SmartDNS, AdGuard Home, sing-box, VPN, специальные nftables/PBR rules, Gitea/Jenkins, базы данных, reverse proxy и service-specific users/directories.
