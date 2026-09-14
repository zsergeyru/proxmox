# Политика сборки и базовой настройки `tpl-debian13`

Этот документ фиксирует согласованные решения для базового Debian 13 template в Proxmox.

Текущая значимая версия базовой политики:

```text
Template-Version: 2
```

## 1. Главный принцип: Proxmox-хост остаётся чистым

На Proxmox-хост **не устанавливаем `virt-customize`, `libguestfs-tools` и другие build-инструменты**, которые нужны только для подготовки гостевой ОС.

Сборка выполняется через временную VM:

```text
Debian 13 genericcloud image
        ↓
временная builder-VM в Proxmox
        ↓
Cloud-Init первого запуска
        ↓
apt update / full-upgrade
установка базовых пакетов
настройка ОС
очистка machine-specific данных
        ↓
shutdown
        ↓
стандартные Cloud-Init defaults для клонов
        ↓
преобразование в tpl-debian13
```

После успешной подготовки отдельная builder-VM не сохраняется как самостоятельная рабочая машина: подготовленная VM становится template.

## 2. Что разрешено использовать на Proxmox-хосте

Скрипт должен опираться прежде всего на штатные средства Proxmox и базовой Debian-системы хоста:

- `qm`;
- `pvesm`;
- Cloud-Init support Proxmox;
- стандартные средства скачивания и проверки образа;
- штатные операции импорта диска и управления VM.

Если для новой реализации потребуется дополнительный пакет на PVE-хосте, это считается отдельным архитектурным решением и не должно происходить автоматически.

## 3. Базовый пользователь

Основной административный пользователь рабочих Linux VM:

```text
ops
```

`ops` используется для:

- SSH-входа по ключу;
- входа через Proxmox serial console по паролю;
- ручного администрирования;
- `sudo`;
- работы с Git, конфигурацией и логами.

Базовые группы:

```text
ops
sudo
adm
```

Специальные группы (`docker`, `www-data`, `backup` и т. п.) добавляются только на конкретных VM по необходимости.

## 4. Root

Учётная запись `root` как системная часть Debian **не удаляется**.

Удалённый SSH-login для `root` полностью запрещается:

```text
PermitRootLogin no
```

Прямой пользовательский вход под root не является штатным способом администрирования. Аварийный доступ через Proxmox console выполняется пользователем `ops`, после чего административные команды запускаются через `sudo`.

## 5. Политика доступа

Целевая модель состоит из двух каналов:

```text
Proxmox serial console
→ ops + password

SSH
→ ops + private key на административном компьютере
```

SSH-политика:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Наличие password у `ops` не включает password authentication в SSH.

## 6. Console password

При сборке template скрипт интерактивно запрашивает пароль пользователя `ops` для console login.

Требования:

- пароль не записывается в Git;
- пароль не должен вводиться в командной строке открытым текстом;
- скрипт запрашивает его скрыто;
- template получает этот пароль как стандартный Cloud-Init default для клонов;
- обычные клоны по умолчанию наследуют тот же console password;
- при необходимости password конкретной VM можно заменить через её Cloud-Init настройки.

Общий console password для клонов является осознанным упрощением домашней инфраструктуры и не отменяет SSH key authentication.

## 7. SSH public key по умолчанию

Административный private SSH key хранится только на пользовательском компьютере.

При сборке `create-template.sh` запрашивает только public key и записывает его в стандартную Cloud-Init конфигурацию template.

В результате обычный клон сразу получает:

```text
/home/ops/.ssh/authorized_keys
```

с согласованным public key.

Public key не является секретом и при необходимости может также храниться в Git. Private key в template и на Proxmox-хосте не хранится.

Подробная модель описана в [`users-and-keys.md`](./users-and-keys.md).

## 8. Cloud-Init

Cloud-Init используется для первоначальной персонализации VM и bootstrap-подготовки template.

После завершения builder-подготовки custom Cloud-Init удаляется, а для template задаются стандартные defaults:

```text
ciuser = ops
console password = заданный при сборке
SSH public key = заданный при сборке
network = DHCP
```

Для конкретных рабочих клонов Cloud-Init может дополнительно задавать:

- hostname;
- другой password;
- дополнительные SSH public keys;
- IP/network configuration;
- DNS и другие параметры первого запуска.

Cloud-Init не должен превращаться в систему конфигурационного управления прикладными сервисами. SmartDNS, Docker, Gitea, monitoring и другие роли устанавливаются отдельными deploy/Ansible/script механизмами после клонирования.

## 9. Временная зона и локаль

Временная зона:

```text
Europe/Moscow
```

Основная локаль:

```text
en_US.UTF-8
```

Системное время синхронизируется штатными средствами Linux.

## 10. Обновление системы при сборке

При создании template выполняется:

```text
apt update
apt full-upgrade
```

После этого устанавливается согласованный базовый набор утилит.

`unattended-upgrades` автоматически не включается.

## 11. Swap

В base template отдельный swap-раздел или swap-файл **не создаётся**.

При необходимости swap добавляется конкретной VM.

## 12. Виртуальный диск

Базовые параметры:

```text
Controller: VirtIO SCSI Single
iothread:   enabled
discard:    enabled
ssd:        enabled
size:       16 GiB
```

После клонирования диск можно увеличить под конкретную роль.

## 13. QEMU Guest Agent

`qemu-guest-agent` обязательно входит в template и включается как внутри Debian, так и в конфигурации VM Proxmox.

Он используется для:

- корректного shutdown;
- получения информации о гостевой системе;
- IP-информации;
- guest diagnostics;
- поддерживаемых backup-операций.

## 14. Serial console

Template должен иметь рабочую serial console:

```text
serial0: socket
vga: serial0
```

Внутри гостя должен работать `serial-getty@ttyS0.service`.

Console является аварийным каналом управления VM и использует вход `ops` + password.

## 15. Очистка перед template

Перед финальным shutdown и `qm template` очищаются machine-specific и временные данные:

- Cloud-Init state;
- `/etc/machine-id`;
- SSH host keys;
- DHCP lease/state;
- random seed;
- APT cache/lists;
- временные файлы;
- builder artifacts;
- build journal/logs;
- shell history процесса сборки.

При первом запуске клона должны сформироваться собственные machine-id и SSH host keys.

## 16. Shell history

Для административных пользователей включаются timestamps команд и увеличенный разумный history size через `/etc/profile.d/`.

История не должна использоваться для хранения секретов.

## 17. MOTD

Template получает короткий нейтральный MOTD:

```text
Managed VM
Base template: tpl-debian13
Infrastructure: zsergeyru/proxmox
Do not store secrets in Git.
```

## 18. Информация о происхождении template

В template создаётся:

```text
/etc/vm-template-info
```

с содержимым вида:

```text
Template: tpl-debian13
Template-Version: 2
OS: Debian 13
Source: zsergeyru/proxmox
Build-Date: <дата фактической сборки>
```

Версию повышаем, когда меняется значимая базовая политика или состав образа.

## 19. Структура файлов сервисов

Template не создаёт заранее каталоги будущих сервисов.

Правила `/opt`, `/etc`, `/var/lib`, `/srv`, `/var/log`, `/var/cache`, `/run` и `/tmp` описаны в [`filesystem-layout.md`](./filesystem-layout.md).

## 20. Backup

На первом этапе backup остаётся простым:

- полные VM backup средствами Proxmox;
- `restic`/`rsync` доступны для файловых сценариев;
- отдельный secret-backup пока не создаётся;
- технические private SSH keys внутри VM попадают в полный backup этой VM.

Backup, содержащий secrets, считается чувствительным объектом и хранится только на доверенном storage.

## 21. Что не входит в base template

Не устанавливаем заранее:

- Docker Engine / Docker Compose;
- SmartDNS;
- AdGuard Home;
- `sing-box`;
- AmneziaWG и другие VPN;
- специальные `nftables`/policy-routing правила;
- Gitea;
- Jenkins;
- базы данных;
- reverse proxy;
- пользователей и каталоги прикладных сервисов.

## 22. Требования к `create-template.sh`

Скрипт должен:

1. не использовать `virt-customize`;
2. не устанавливать `libguestfs-tools` на Proxmox;
3. выполнять подготовку Debian внутри временной VM;
4. не хранить private SSH keys, passwords или tokens в Git;
5. интерактивно получать console password и SSH public key;
6. оставлять SSH password authentication выключенным;
7. задавать public key и console password через стандартные Proxmox Cloud-Init defaults template;
8. после успешной подготовки преобразовывать VM в `tpl-debian13`;
9. при опасной или неоднозначной ситуации останавливаться, а не удалять существующие VM/storage автоматически.
