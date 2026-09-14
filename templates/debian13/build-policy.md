# Политика сборки и базовой настройки `tpl-debian13`

```text
Template-Version: 3
```

## 1. Proxmox-хост остаётся чистым

Не устанавливаем на PVE `virt-customize`, `libguestfs-tools` и другие build-инструменты, нужные только для подготовки гостевой ОС.

Сборка выполняется штатными средствами Proxmox и временной builder-VM.

## 2. Источник образа и проверка

Используется официальный Debian 13 Trixie genericcloud image.

Перед импортом обязательно скачивается официальный `SHA512SUMS` и проверяется SHA-512 образа. Использованный hash записывается в `/etc/vm-template-info`.

Скрипт не продолжает сборку, если checksum отсутствует, имеет неверный формат или не совпадает.

## 3. Базовый пользователь

Основной административный пользователь:

```text
ops
```

Группы:

```text
ops
sudo
adm
```

Sudo policy задаётся отдельным файлом:

```text
/etc/sudoers.d/90-ops
ops ALL=(ALL:ALL) NOPASSWD:ALL
```

Конфигурация проверяется через `visudo -cf`.

## 4. Пароли и SSH

Пароль `ops` остаётся locked. Пустой пароль не используется.

SSH policy:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Файл должен называться:

```text
/etc/ssh/sshd_config.d/00-template-security.conf
```

Конфигурация проверяется через `sshd -t`.

## 5. Доверенная Proxmox console

Начиная с v3 основной рабочий канал VM console — текстовая serial-консоль:

```text
serial0: socket
Proxmox xterm.js → ttyS0 → autologin ops
```

При этом виртуальная VGA не отключается:

```text
vga: std
noVNC → VGA/tty1
```

То есть `xterm.js` используется как основной административный интерфейс, а noVNC/VGA сохраняется как независимый резервный канал. На `tty1` специальный autologin не настраивается.

На `ttyS0` используется:

```text
agetty --autologin ops
```

Пароль при этом не разблокируется. Право Proxmox `VM.Console` для такой VM следует считать административным доступом к гостевой ОС, поскольку `ops` может выполнять `sudo` без пароля.

## 6. Cloud-Init lifecycle

Временный custom Cloud-Init используется только при сборке builder-VM.

Host-сценарий обязан:

1. дождаться QEMU Guest Agent;
2. дождаться маркера завершения bootstrap;
3. дополнительно дождаться `cloud-init status --wait`;
4. только после этого запускать final cleanup;
5. выполнить `cloud-init clean --logs --seed` без игнорирования ошибок;
6. удалить builder-only `cicustom`;
7. задать стандартные defaults template;
8. выполнить `qm cloudinit update`;
9. проверить, что builder-only user-data больше не присутствует.

Для template:

```text
ciuser=ops
ipconfig0=ip=dhcp
ciupgrade=0
```

`cipassword` штатно не используется.

## 7. Порядок персонализации клона

Для каждого Full Clone параметры Cloud-Init задаются **до первого старта**:

```text
clone
→ name/hostname
→ ciuser=ops
→ sshkeys
→ network/DNS
→ qm cloudinit update
→ start
```

SSH key после первого boot добавлять не следует как штатный сценарий: Cloud-Init предназначен для первичной персонализации.

## 8. Обновления

Builder выполняет:

```text
apt update
apt full-upgrade
```

Template получает актуальные пакеты на дату сборки.

Автоматический package upgrade клонов через Cloud-Init выключен:

```text
ciupgrade=0
```

`unattended-upgrades` автоматически не включается.

## 9. Время

Устанавливается и включается `systemd-timesyncd`.

```text
Timezone: Europe/Moscow
Locale: en_US.UTF-8
```

Ошибку включения NTP не следует молча игнорировать.

## 10. Диск, growpart и TRIM

```text
Controller: VirtIO SCSI Single
iothread: enabled
discard: enabled
ssd: enabled
base size: 16 GiB
```

В template должен быть `cloud-guest-utils`, чтобы `growpart` был доступен клонам после увеличения диска.

Включается `fstrim.timer`. Перед финальным shutdown выполняется `fstrim -av`; ошибка fstrim логируется как предупреждение и не считается причиной провала сборки.

## 11. QEMU Guest Agent

`qemu-guest-agent` обязателен. Он используется для shutdown, IP/status, guest exec и части диагностических сценариев.

## 12. Очистка machine-specific данных

Перед `qm template` удаляются:

- Cloud-Init state/logs/seed;
- machine-id и dbus machine-id;
- SSH host keys;
- DHCP/network state;
- random seed;
- APT cache/lists;
- journal/build logs;
- temporary files;
- shell history;
- `/home/ops/.ssh`;
- `/usr/local/sbin/template-bootstrap`;
- `/usr/local/sbin/template-finalize`.

Настройки serial0/xterm.js autologin, VGA/noVNC fallback, SSH hardening, sudo policy, timesync и fstrim являются частью base template и не удаляются.

## 13. Информация о происхождении

`/etc/vm-template-info` содержит:

```text
Template: tpl-debian13
Template-Version: <TEMPLATE_VERSION>
OS: Debian 13
Infrastructure-Source: zsergeyru/proxmox
Bootstrap-Source: zsergeyru/proxmox-bootstrap
Source-Image: <image filename>
Source-Image-SHA512: <sha512>
Build-Date: <UTC date>
```

Версия подставляется из одной переменной `TEMPLATE_VERSION`, а не дублируется жёстко в нескольких местах.

## 14. Финальные проверки

Перед успешным завершением скрипт проверяет конфигурацию template:

```text
template: 1
protection: 1
vga: std
serial0: socket
ciupgrade: 0
```

Builder-only Cloud-Init не должен остаться в стандартном Cloud-Init drive.

## 15. Защита template

После успешного `qm template` устанавливается:

```text
protection=1
```

Цель — защита от случайного удаления VMID 9000. Это не полноценная security boundary: пользователь с достаточными правами Proxmox способен снять protection.

## 16. Поведение при ошибке

Скрипт никогда не уничтожает автоматически существующую VM или storage.

При ошибке builder сохраняется для диагностики. `trap ERR` остаётся активным до успешного завершения `qm template`, включения protection и финальных проверок.

## 17. Что не входит в base template

Не устанавливаются заранее Docker/Compose, SmartDNS, AdGuard Home, sing-box, VPN, специальные nftables/PBR rules, Gitea/Jenkins, базы данных, reverse proxy и service-specific users/directories.
