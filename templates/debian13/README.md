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
- [`create-template.sh`](./create-template.sh) — фактическая автоматизированная сборка.

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
настройка стандартного Cloud-Init:
ops + console password + SSH public key + DHCP
        ↓
qm template
        ↓
tpl-debian13
```

Скрипт проверяет SHA-512 образа по официальному `SHA512SUMS` Debian.

При ошибке builder-VM **не удаляется автоматически**, чтобы можно было проверить console, Cloud-Init и логи. Существующий VMID `9000` скрипт никогда не перезаписывает.

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
| Console login | `ops` + пароль |
| Root SSH | запрещён |
| Password SSH | запрещён |
| SSH | public key |
| Timezone | `Europe/Moscow` |
| Locale | `en_US.UTF-8` |
| Swap | не создаётся |
| Template-Version | `2` |

## Доступ к клонам

В версии 2 шаблон получает два заранее настроенных способа доступа:

```text
Proxmox serial console
→ ops + password

SSH
→ ops + private key на компьютере
→ public key в Cloud-Init template
```

Пароль используется для локальной/Proxmox-консоли, но парольный SSH остаётся запрещённым:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Во время сборки скрипт интерактивно запрашивает:

1. SSH public key администратора;
2. пароль `ops` для console login;
3. повтор пароля.

Пароль и private SSH key **не хранятся в Git**.

Public key не является секретом. Скрипт записывает его в стандартные Cloud-Init параметры template, поэтому обычные клоны наследуют этот ключ автоматически.

Console password также становится Cloud-Init default template. Поэтому клоны по умолчанию получают один и тот же console password. При необходимости его можно заменить на конкретной VM через Cloud-Init.

## Требования перед запуском

На Proxmox должны быть доступны:

```text
qm
pvesm
sha512sum
curl или wget
mktemp
```

Storage `local` должен поддерживать content type **Snippets**, потому что custom Cloud-Init нужен только на время сборки builder-VM.

Если `Snippets` выключен:

```text
Datacenter → Storage → local → Edit → Content → Snippets
```

Скрипт сам не меняет конфигурацию storage.

VMID `9000` должен быть свободен.

Перед сборкой необходимо иметь SSH key pair на административном компьютере. В скрипт вставляется только содержимое файла `.pub`.

## Запуск

```bash
cd /path/to/proxmox/templates/debian13
chmod +x create-template.sh
sudo ./create-template.sh
```

Скрипт попросит public key и console password, после чего выполнит сборку.

По умолчанию используются:

```text
VMID=9000
DISK_STORAGE=local-lvm
SNIPPET_STORAGE=local
BRIDGE=vmbr0
DISK_SIZE=16G
```

При необходимости значения можно переопределить без изменения скрипта:

```bash
DISK_STORAGE=local-lvm BRIDGE=vmbr0 ./create-template.sh
```

URL cloud image тоже можно переопределить через `IMAGE_URL` и `CHECKSUM_URL`, но штатный сценарий использует latest Debian 13 Trixie genericcloud image.

## Базовые пакеты

В template устанавливаются:

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

Обычное администрирование:

```text
SSH → ops → sudo
```

Аварийный доступ без сети:

```text
Proxmox serial console → ops → password → sudo
```

`root` как системная учётная запись Debian сохраняется, но прямой SSH-login root запрещён. Основной console login также выполняется через `ops`, а не через root.

## Очистка перед template

Перед `qm template` внутри builder выполняется очистка:

- Cloud-Init state;
- machine-id;
- SSH host keys;
- DHCP lease/state;
- random seed;
- APT cache/lists;
- временные файлы;
- journal/build logs;
- shell history процесса сборки.

Каждый клон должен сформировать собственные machine-id и SSH host keys.

## Проверка после сборки

Перед использованием template для инфраструктуры рекомендуется создать один тестовый **Full Clone** и проверить:

1. загрузку VM;
2. получение DHCP;
3. создание нового machine-id;
4. создание уникальных SSH host keys;
5. вход `ops` через Proxmox console по паролю;
6. вход `ops` по SSH-ключу без пароля;
7. работу `sudo`;
8. QEMU Guest Agent;
9. serial console;
10. корректный размер root filesystem;
11. содержимое `/etc/vm-template-info`.

После этой проверки template можно использовать для `109-network-gateway`, `301-ai-control` и других Debian VM.

## Что не входит в base template

Не устанавливаются заранее:

- Docker Engine / Compose;
- SmartDNS / AdGuard Home;
- sing-box;
- AmneziaWG и другие VPN;
- специальные nftables/PBR rules;
- Gitea/Jenkins;
- базы данных;
- reverse proxy;
- service-specific users и directories.
