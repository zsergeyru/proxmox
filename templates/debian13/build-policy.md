# Политика сборки и базовой настройки `tpl-debian13`

```text
Template-Version: 4
```

## 1. Proxmox-хост остаётся чистым

Не устанавливаем на PVE `virt-customize`, `libguestfs-tools`, Git, Docker и другие build-инструменты, нужные только для подготовки гостевой ОС. Сборка выполняется штатными средствами Proxmox и временной builder-VM.

## 2. Источник образа и проверка

Используется официальный Debian 13 Trixie `genericcloud` image. Перед импортом скачивается официальный `SHA512SUMS` и проверяется SHA-512 образа. Использованный hash записывается в `/etc/vm-template-info`.

Скрипт не продолжает сборку, если checksum отсутствует, имеет неверный формат или не совпадает.

## 3. Ядро и VGA console

Исходный genericcloud image содержит `cloud-amd64` kernel. На наших VM он даёт VGA text mode без framebuffer, из-за чего noVNC показывает слишком крупный текст.

Поэтому v4 во время bootstrap:

```text
install linux-image-amd64
install console-setup + console-setup-linux
remove linux-image-*cloud-amd64
update-grub
```

Настройка консоли:

```text
ACTIVE_CONSOLES="/dev/tty[1-6]"
CHARMAP="UTF-8"
CODESET="CyrSlav"
FONTFACE="Fixed"
FONTSIZE="8x16"
```

После bootstrap builder обязательно перезагружается. Сборка продолжается только если после reboot одновременно выполнены условия:

```text
uname -r → обычный *-amd64 kernel, без cloud
/sys/class/graphics/fb0/virtual_size → 1280,800
QEMU Guest Agent отвечает
getty@tty1 active
serial-getty@ttyS0 active
```

Это не косметическая настройка: verification reboot гарантирует, что template реально загружается с тем ядром и VGA-режимом, которые будут использовать клоны.

## 4. Базовый пользователь

Основной административный пользователь:

```text
ops
```

Группы: `ops`, `sudo`, `adm`.

Sudo policy:

```text
/etc/sudoers.d/90-ops
ops ALL=(ALL:ALL) NOPASSWD:ALL
```

Пароль `ops` остаётся locked. Пустой пароль не используется.

## 5. SSH

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Файл:

```text
/etc/ssh/sshd_config.d/00-template-security.conf
```

Конфигурация проверяется через `sshd -t`. Персональных SSH-ключей в base template нет; public key задаётся конкретному клону через Cloud-Init до первого запуска.

## 6. Доверенная Proxmox console

Начиная с v4 штатная Web Console снова использует обычный VGA/noVNC:

```text
vga: std
Proxmox Console → noVNC/VGA → tty1 → autologin ops
```

Autologin `tty1` задаётся через:

```text
/etc/systemd/system/getty@tty1.service.d/autologin.conf
```

Параллельно сохраняется резервная serial-консоль:

```text
serial0: socket
xterm.js / qm terminal → ttyS0 → autologin ops
```

Её override:

```text
/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
```

Пароль при этом не разблокируется. Право Proxmox `VM.Console` следует считать административным доступом к гостевой ОС, потому что `ops` имеет `NOPASSWD: sudo`.

## 7. Cloud-Init lifecycle

Временный custom Cloud-Init используется только при сборке builder-VM.

Host-сценарий обязан:

1. дождаться QEMU Guest Agent;
2. дождаться маркера завершения bootstrap;
3. дождаться `cloud-init status --wait`;
4. выполнить verification reboot в обычное Debian-ядро;
5. проверить kernel/framebuffer/QGA/tty1/ttyS0;
6. выполнить final cleanup;
7. удалить builder-only `cicustom`;
8. задать стандартные defaults template;
9. выполнить `qm cloudinit update`;
10. проверить, что builder-only user-data больше не присутствует.

Для template:

```text
ciuser=ops
ipconfig0=ip=dhcp
ciupgrade=0
```

`cipassword` штатно не используется.

## 8. Порядок персонализации клона

Для каждого рабочего VM используется **Full Clone**. Параметры задаются до первого старта:

```text
Full Clone
→ protection=0 по умолчанию для обычного клона
→ name/hostname
→ ciuser=ops
→ sshkeys
→ network/DNS
→ qm cloudinit update
→ start
```

Template `9000` остаётся `protection=1`. Proxmox может перенести protection в конфигурацию клона, поэтому deploy-сценарий/AI-агент обязан явно выставлять `protection=0`, если паспорт конкретной VM не требует защиты.

Linked Clone не используется для обычных рабочих VM. Если инструмент автоматизации утверждает, что создал Full Clone, результат следует проверять по Proxmox task log или LVM `Origin`: у основного диска Full Clone поле `Origin` должно быть пустым.

## 9. Обновления

Builder выполняет:

```text
apt update
apt full-upgrade
```

Template получает актуальные пакеты на дату сборки. Автоматический package upgrade клонов через Cloud-Init выключен (`ciupgrade=0`). `unattended-upgrades` автоматически не включается.

## 10. Время

Устанавливается и включается `systemd-timesyncd`.

```text
Timezone: Europe/Moscow
Locale: en_US.UTF-8
```

## 11. Диск, growpart и TRIM

```text
Controller: VirtIO SCSI Single
iothread: enabled
discard: enabled
ssd: enabled
base size: 16 GiB
```

В template установлен `cloud-guest-utils`, чтобы root partition/filesystem клона расширялись после увеличения виртуального диска. Включён `fstrim.timer`; перед финальным shutdown выполняется `fstrim -av`.

## 12. QEMU Guest Agent

`qemu-guest-agent` обязателен. Он используется для build lifecycle, shutdown, status/IP, guest exec и диагностики. После verification reboot builder обязан снова ответить на QGA ping; иначе template не создаётся.

## 13. Очистка machine-specific данных

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

Настройки VGA/noVNC tty1 autologin, serial0 fallback, console font, SSH hardening, sudo policy, timesync и fstrim являются частью base template и не удаляются.

## 14. Информация о происхождении

`/etc/vm-template-info` содержит как минимум:

```text
Template: tpl-debian13
Template-Version: <TEMPLATE_VERSION>
OS: Debian 13
Kernel-Flavor: amd64
Primary-Console: VGA/noVNC tty1 autologin ops
Fallback-Console: serial0 ttyS0 autologin ops
Infrastructure-Source: zsergeyru/proxmox
Bootstrap-Source: zsergeyru/proxmox-bootstrap
Source-Image: <image filename>
Source-Image-SHA512: <sha512>
Build-Date: <UTC date>
```

## 15. Финальные проверки

Перед успешным завершением builder проверяет конфигурацию template:

```text
template: 1
protection: 1
vga: std
serial0: socket
ciupgrade: 0
```

Builder-only Cloud-Init не должен остаться в стандартном Cloud-Init drive.

## 16. Защита template и клонов

Base template VMID `9000`:

```text
protection=1
```

Обычный рабочий Full Clone:

```text
protection=0
```

Защита включается на конкретной VM только по явному решению.

## 17. Поведение при ошибке

Скрипт никогда автоматически не уничтожает существующую VM или storage. При ошибке builder сохраняется для диагностики. `trap ERR` остаётся активным до успешного `qm template`, включения protection и финальных проверок.

## 18. Что не входит в base template

Не устанавливаются заранее Docker/Compose, SmartDNS, AdGuard Home, sing-box, VPN, специальные nftables/PBR rules, Gitea/Jenkins, базы данных, reverse proxy и service-specific users/directories.
