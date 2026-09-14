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

Обычное администрирование:

```text
SSH → ops → sudo
```

## 5. Credentials не входят в template

Base template **не содержит**:

- личных SSH public keys;
- private SSH keys;
- рабочего пароля `ops`;
- других персональных credentials.

Пользователь `ops` существует в template, но его пароль остаётся заблокированным до персонализации конкретного клона.

Это позволяет одному template обслуживать разные VM, устройства и наборы ключей без пересборки.

## 6. Cloud-Init конкретного клона

После Full Clone через стандартный Proxmox Cloud-Init задаются:

```text
ciuser = ops
cipassword = пароль конкретной VM для console login
sshkeys = один или несколько public keys
ipconfig0 = DHCP или статический адрес
```

Также через Cloud-Init задаются hostname, DNS и другие параметры первого запуска при необходимости.

Пароль `ops` нужен для входа через Proxmox serial console. Несмотря на наличие системного пароля, SSH по паролю остаётся запрещённым настройкой `sshd`.

Публичный SSH-ключ конкретного устройства попадает в:

```text
/home/ops/.ssh/authorized_keys
```

## 7. Временная зона и локаль

```text
Timezone: Europe/Moscow
Locale:   en_US.UTF-8
```

Системное время синхронизируется штатными средствами Linux.

## 8. Обновление при сборке

В builder-VM выполняется:

```text
apt update
apt full-upgrade
```

После этого устанавливается согласованный базовый набор пакетов.

`unattended-upgrades` автоматически не включается.

## 9. Swap

В base template swap не создаётся. При необходимости он добавляется конкретной VM.

## 10. Виртуальный диск

```text
Controller: VirtIO SCSI Single
iothread:   enabled
discard:    enabled
ssd:        enabled
size:       16 GiB
```

## 11. QEMU Guest Agent

`qemu-guest-agent` обязателен и включается внутри Debian и в конфигурации VM Proxmox.

## 12. Serial console

```text
serial0: socket
vga: serial0
```

Внутри гостя должен быть включён `serial-getty@ttyS0.service`.

После задания `cipassword` конкретному клону console login выполняется как:

```text
ops + password
```

## 13. Очистка перед template

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

## 14. Shell history и MOTD

Через `/etc/profile.d/` включаются timestamps истории и увеличенный history size.

MOTD:

```text
Managed VM
Base template: tpl-debian13
Infrastructure: zsergeyru/proxmox
Do not store secrets in Git.
```

## 15. Информация о template

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

## 16. Backup

На первом этапе:

- полный VM backup средствами Proxmox;
- `restic`/`rsync` доступны для файловых сценариев;
- отдельный secret-backup не создаётся;
- technical private keys внутри VM попадают в её полный backup.

Backup с секретами считается чувствительным объектом.

## 17. Что не входит в base template

Не устанавливаются заранее Docker/Compose, SmartDNS, AdGuard Home, sing-box, VPN, специальные nftables/PBR rules, Gitea/Jenkins, базы данных, reverse proxy и service-specific users/directories.

## 18. Требования к `create-template.sh`

Скрипт должен:

1. не использовать `virt-customize`;
2. не устанавливать `libguestfs-tools` на Proxmox;
3. готовить Debian внутри временной VM;
4. не запрашивать и не сохранять credentials конкретного администратора;
5. оставлять `ops` без рабочего пароля и без `authorized_keys` в самом template;
6. сохранять `ciuser=ops` и DHCP как нейтральные defaults;
7. ожидать, что `cipassword`, `sshkeys`, hostname/IP задаются конкретному клону через Cloud-Init;
8. оставлять SSH password authentication выключенным;
9. при ошибке не удалять существующие VM/storage автоматически.
