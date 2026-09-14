# Политика сборки и базовой настройки `tpl-debian13`

Этот документ фиксирует согласованные решения для базового Debian 13 template в Proxmox.

```text
Template-Version: 2
```

## 1. Главный принцип

Proxmox-хост остаётся максимально чистым. Не устанавливаем `virt-customize`, `libguestfs-tools` и другие build-инструменты, нужные только для подготовки гостевой ОС.

Сборка выполняется через временную VM:

```text
Debian 13 genericcloud image
        ↓
builder-VM
        ↓
Cloud-Init первого запуска
        ↓
apt update / full-upgrade
установка базовых пакетов
настройка ОС
настройка tty1 autologin
очистка machine-specific данных
        ↓
shutdown
        ↓
qm template
```

## 2. Средства Proxmox-хоста

Скрипт использует штатные средства:

- `qm`;
- `pvesm`;
- Cloud-Init support Proxmox;
- `curl` или `wget`;
- `sha512sum`;
- штатный импорт дисков и управление VM.

Дополнительные пакеты на PVE-хост автоматически не устанавливаются.

## 3. Пользователь `ops`

Основной административный пользователь Linux VM:

```text
ops
```

Базовые группы:

```text
ops
sudo
adm
```

`ops` имеет:

```text
ALL=(ALL) NOPASSWD:ALL
```

Специальные группы добавляются только по роли конкретной VM.

## 4. Root и SSH

`root` не удаляется, но удалённый вход полностью запрещён:

```text
PermitRootLogin no
```

SSH-политика:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Обычное сетевое администрирование:

```text
SSH → ops → sudo
```

## 5. Credentials не входят в template

Base template **не содержит**:

- личных SSH public keys;
- private SSH keys;
- рабочего пароля `ops`;
- других персональных credentials.

Пользователь `ops` существует в template, его пароль заблокирован и остаётся заблокированным в обычных клонах.

Это позволяет одному template обслуживать разные VM, устройства и наборы ключей без пересборки.

## 6. Cloud-Init конкретного клона

После Full Clone через стандартный Proxmox Cloud-Init задаются:

```text
ciuser = ops
sshkeys = один или несколько public keys
ipconfig0 = DHCP или статический адрес
```

Также через Cloud-Init задаются hostname, DNS и другие параметры первого запуска при необходимости.

`cipassword` в штатном сценарии не используется.

Публичный SSH-ключ конкретного устройства или роли попадает в:

```text
/home/ops/.ssh/authorized_keys
```

## 7. Доверенный доступ через Proxmox console

Proxmox Web UI уже имеет собственную аутентификацию и ACL. Для VM console принимается следующая модель доверия:

```text
право открыть console VM в Proxmox
= право получить локальный shell ops внутри этой VM
```

Это реализуется не пустым паролем и не передачей Proxmox credentials в Debian, а локальным autologin на `tty1`:

```text
getty@tty1
→ agetty --autologin ops
→ shell ops
```

Пароль `ops` при этом остаётся **locked**.

Следствие: пользователь Proxmox, которому выдано право на console конкретной VM, фактически получает административный доступ к гостевой ОС, потому что `ops` имеет `NOPASSWD: sudo`. Это осознанная trust boundary проекта.

## 8. VGA/noVNC и serial console

Основная интерактивная console:

```text
vga: std
```

Кнопка `VM → Console` в Proxmox должна открывать noVNC с обычной Linux text console `tty1`.

Внутри гостя должен быть включён:

```text
getty@tty1.service
```

с override:

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ops --noclear %I $TERM
```

Одновременно сохраняется:

```text
serial0: socket
```

и `serial-getty@ttyS0.service` как резервный диагностический канал. На `serial0` autologin **не настраивается**.

## 9. Временная зона и локаль

```text
Timezone: Europe/Moscow
Locale:   en_US.UTF-8
```

Системное время синхронизируется штатными средствами Linux.

## 10. Обновление при сборке

В builder-VM выполняется:

```text
apt update
apt full-upgrade
```

После этого устанавливается согласованный базовый набор пакетов.

`unattended-upgrades` автоматически не включается.

## 11. Swap

В base template swap не создаётся. При необходимости он добавляется конкретной VM.

## 12. Виртуальный диск

```text
Controller: VirtIO SCSI Single
iothread:   enabled
discard:    enabled
ssd:        enabled
size:       16 GiB
```

## 13. QEMU Guest Agent

`qemu-guest-agent` обязателен и включается внутри Debian и в конфигурации VM Proxmox.

Кроме штатного shutdown и получения сетевой информации, Guest Agent используется как дополнительный аварийный канал управления через `qm guest exec`.

## 14. Очистка перед template

Перед финальным shutdown очищаются:

- Cloud-Init state;
- `/etc/machine-id`;
- SSH host keys;
- DHCP/network state;
- random seed;
- APT cache/lists;
- временные файлы;
- build artifacts и build logs;
- shell history;
- `/home/ops/.ssh`.

Клон должен сформировать собственные machine-id и SSH host keys.

Настройка `getty@tty1` autologin является частью базовой политики template и при очистке не удаляется.

## 15. Shell history и MOTD

Через `/etc/profile.d/` включаются timestamps истории и увеличенный history size.

MOTD:

```text
Managed VM
Base template: tpl-debian13
Infrastructure: zsergeyru/proxmox
Do not store secrets in Git.
```

## 16. Информация о template

```text
/etc/vm-template-info
```

содержит:

```text
Template: tpl-debian13
Template-Version: 2
OS: Debian 13
Source: zsergeyru/proxmox
Build-Date: <дата сборки>
```

## 17. Backup

На первом этапе:

- полный VM backup средствами Proxmox;
- `restic`/`rsync` доступны для файловых сценариев;
- отдельный secret-backup не создаётся;
- technical private keys внутри VM попадают в её полный backup.

Backup с секретами считается чувствительным объектом.

## 18. Что не входит в base template

Не устанавливаются заранее Docker/Compose, SmartDNS, AdGuard Home, sing-box, VPN, специальные nftables/PBR rules, Gitea/Jenkins, базы данных, reverse proxy и service-specific users/directories.

## 19. Требования к `create-template.sh`

Скрипт должен:

1. не использовать `virt-customize`;
2. не устанавливать `libguestfs-tools` на Proxmox;
3. готовить Debian внутри временной VM;
4. не запрашивать и не сохранять credentials конкретного администратора;
5. оставлять `ops` без рабочего пароля и без `authorized_keys` в самом template;
6. сохранять `ciuser=ops` и DHCP как нейтральные defaults;
7. ожидать, что `sshkeys`, hostname/IP задаются конкретному клону через Cloud-Init;
8. не использовать `cipassword` в штатном deploy-сценарии;
9. оставлять SSH password authentication выключенным;
10. использовать `vga: std` для основной noVNC console;
11. настраивать autologin `ops` только на `tty1`;
12. сохранять `serial0: socket` без autologin как резервный диагностический канал;
13. при ошибке не удалять существующие VM/storage автоматически.
