# Базовый шаблон Debian 13 для Proxmox

Статус: **политика template согласована; финальный скрипт ещё должен быть переписан под сборку без `virt-customize`**.

> Старая версия `create-template.sh`, использовавшая `virt-customize/libguestfs-tools`, признана устаревшей. До её замены она не должна использоваться для создания template.

## Назначение

Базовый template — универсальная основа для Linux VM в Proxmox:

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
```

Рабочие VM по умолчанию создаются как **Full Clone**.

Template содержит только общие настройки ОС и базовые утилиты. Docker, SmartDNS, VPN, Gitea, Jenkins, базы данных и другие прикладные роли устанавливаются после клонирования.

## Документация

- [`build-policy.md`](./build-policy.md) — окончательная политика сборки и базовых настроек;
- [`users-and-keys.md`](./users-and-keys.md) — пользователи, SSH-ключи и backup ключей;
- [`filesystem-layout.md`](./filesystem-layout.md) — назначение `/opt`, `/etc`, `/var/lib`, `/srv`, `/var/log`, `/var/cache`, `/run`, `/tmp`.

Эти документы являются источником истины для финальной версии скрипта.

## Базовые параметры

| Параметр | Значение |
|---|---|
| CPU | 1 vCPU, type `host` |
| RAM | 1 GiB |
| System disk | 16 GiB |
| Controller | VirtIO SCSI Single |
| I/O thread | enabled |
| Discard/TRIM | enabled |
| SSD emulation | для SSD-backed storage |
| Network | VirtIO, `vmbr0` |
| Cloud-Init | да |
| QEMU Guest Agent | да |
| Serial console | да |
| Template network | DHCP |
| Admin user | `ops` |
| Root SSH | запрещён |
| Password SSH | запрещён |
| Timezone | `Europe/Moscow` |
| Locale | `en_US.UTF-8` |
| Swap | не создаётся |
| Template-Version | `1` |

После клонирования для конкретной VM задаются hostname, IP, SSH public keys, CPU, RAM и при необходимости увеличивается диск.

## Сборка

Принята схема без `virt-customize` и без установки `libguestfs-tools` на PVE-хост:

```text
Debian genericcloud image
        ↓
временная builder-VM
        ↓
Cloud-Init/bootstrap внутри Debian
        ↓
apt update + apt full-upgrade
установка пакетов и базовая настройка
        ↓
очистка machine-specific данных
        ↓
shutdown
        ↓
qm template
```

Proxmox-хост должен оставаться максимально чистым.

## Пользователи и SSH

Основной административный пользователь — `ops`, группы:

```text
ops
sudo
adm
```

`root` как системная учётная запись Debian не удаляется, но удалённый вход для него полностью отключается:

```text
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
```

Аварийный root-доступ остаётся через консоль Proxmox.

SSH public keys передаются через Cloud-Init. Private keys в template не хранятся.

## Базовые пакеты

```text
qemu-guest-agent
openssh-server
sudo

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

Порядок размера самих перечисленных пакетов — около **110–120 MiB** без полного учёта зависимостей.

### Назначение групп пакетов

- `qemu-guest-agent`, `openssh-server`, `sudo` — управление VM и SSH;
- `git`, `mc`, `nano`, `tree`, `acl`, `bash-completion` — администрирование файлов и конфигурации;
- `curl`, `wget`, `jq`, `ca-certificates`, `openssl` — HTTP/API/TLS;
- `htop`, `ncdu`, `lsof`, `tmux`, `cron`, `logrotate` — диагностика и обслуживание;
- `dnsutils`, `iproute2`, `iputils-ping`, `net-tools` — сеть и DNS;
- `tar`, `rsync`, `restic`, `zstd`, `unzip` — архивирование и backup.

## Обновления

При сборке нового template выполняются:

```text
apt update
apt full-upgrade
```

`unattended-upgrades` автоматически не включается. Рабочие VM обновляются контролируемо.

## Время

Timezone:

```text
Europe/Moscow
```

Синхронизация времени выполняется штатными средствами Debian/systemd.

## Диск и swap

Базовый диск:

```text
VirtIO SCSI Single
16 GiB
iothread=1
discard=on
```

Для SSD-backed storage допускается `ssd=1`.

Swap в base template не создаётся. При необходимости он добавляется конкретной VM.

## Serial console

Template должен иметь аварийную serial console:

```text
serial0
vga: serial0
```

Она нужна для доступа к VM даже при проблемах с сетью или SSH.

## Очистка перед template

Перед `qm template` очищаются:

- Cloud-Init state;
- machine-id;
- SSH host keys;
- DHCP lease/state;
- APT cache;
- builder/bootstrap temporary files;
- ненужные journal/logs процесса сборки.

Каждый клон должен получить собственные machine-id и SSH host keys.

## Дополнительные базовые настройки

В template предусматриваются:

- timestamp в shell history и увеличенный разумный history size;
- короткий MOTD;
- `/etc/vm-template-info`.

Пример `/etc/vm-template-info`:

```text
Template: tpl-debian13
Template-Version: 1
OS: Debian 13
Source: zsergeyru/proxmox
Build-Date: <actual build date>
```

## Backup

На первом этапе схема простая:

```text
Proxmox backup → VM целиком
restic/rsync   → отдельные файловые сценарии при необходимости
Git            → код и конфигурации без секретов
```

Отдельный secret-backup пока не создаётся. Технические private keys, находящиеся внутри VM, резервируются вместе с полным backup VM.

## Что не входит в base template

Не устанавливаются заранее:

- Docker Engine / Docker Compose;
- SmartDNS / AdGuard Home;
- `sing-box`;
- AmneziaWG / другие VPN;
- специальные `nftables`/policy-routing rules;
- Gitea / Jenkins;
- базы данных;
- reverse proxy;
- service-specific users;
- service-specific directories.

При необходимости позднее можно сделать отдельный производный `tpl-debian13-docker`.

## Требования к финальному `create-template.sh`

1. Не использовать `virt-customize`.
2. Не устанавливать `libguestfs-tools` на Proxmox.
3. Создавать временную builder-VM.
4. Выполнять bootstrap внутри Debian.
5. Использовать `ops` и SSH key-only policy.
6. Использовать `Europe/Moscow`.
7. Включать QEMU Guest Agent, serial console, iothread и discard.
8. Не создавать swap.
9. Очищать machine-specific данные.
10. Не удалять и не перезаписывать существующий VMID автоматически.
11. После успешной подготовки превращать builder-VM в `tpl-debian13`.
