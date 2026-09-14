# Базовый template Debian 13 для Proxmox

## Статус

Текущий baseline:

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 4
Clone policy: Full Clone
Protection: 1
```

Template v4 снова является действующей версией. От усложнения v5 с pinned Debian build, version lock и `snapshot.debian.org` отказались.

Рабочая реализация находится в публичном:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Политика сборки: [`build-policy.md`](build-policy.md).

## Что делает Template v4

```text
Debian 13 trixie/latest generic cloud image
→ SHA-512 verification
→ VMID 9000 builder
→ apt update/full-upgrade из обычных Debian repositories
→ установка base packages
→ regular linux-image-amd64
→ remove cloud-amd64 kernel
→ verification reboot
→ framebuffer + VGA/noVNC tty1 verification
→ QGA + SSH policy + locked-password verification
→ clean machine-specific state
→ standard Cloud-Init defaults
→ qm template
→ protection=1
```

## Источник Debian

Builder использует текущий официальный image:

```text
https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
```

Перед импортом скачивается `SHA512SUMS` из того же каталога и выполняется строгая SHA-512 проверка.

Отдельный фиксированный Debian build и APT snapshot не используются. При новой сборке template получает актуальное на этот момент состояние Debian 13.

## Kernel и console

```text
linux-image-amd64
cloud kernel удалён
vga: std
noVNC/VGA → tty1 → autologin ops
serial0 → ttyS0 → autologin ops (fallback)
Fixed 8x16
```

Builder после reboot требует:

- kernel `*-amd64` без `cloud`;
- существующий framebuffer `fb0` с валидным размером;
- active QEMU Guest Agent;
- active `getty@tty1` и `serial-getty@ttyS0`;
- корректную effective SSH policy.

## Доступ

```text
admin user: ops
ops password: locked
root password: locked
sudo: NOPASSWD
SSH: public key only
Root SSH: disabled
```

Base template не содержит персональных SSH keys. Ключи, сеть и hostname задаются Full Clone до первого запуска через Cloud-Init.

Право Proxmox `VM.Console` считается административным доступом, потому что console autologin приводит к `ops`, имеющему `sudo`.

## Базовые параметры

```text
CPU: 1 vCPU, type host
RAM: 1 GiB
Disk: 16 GiB
Storage: local-lvm
Controller: VirtIO SCSI Single
Network: VirtIO / vmbr0
Template network: DHCP
QEMU Guest Agent: enabled
ciuser: ops
ciupgrade: 0
Timezone: Europe/Moscow
Locale: en_US.UTF-8
```

Docker и application services в base template не устанавливаются.

## Clone lifecycle

```text
Full Clone from 9000
→ CPU/RAM/disk/network
→ ciuser=ops
→ SSH public keys
→ qm cloudinit update
→ start
→ verify QGA/SSH/health
```

Linked Clone не является штатным вариантом.

## `/etc/vm-template-info`

Template v4 записывает:

```text
Template: tpl-debian13
Template-Version: 4
OS: Debian 13
Kernel-Flavor: amd64
Source-Image: debian-13-genericcloud-amd64.qcow2
Source-Image-SHA512: <verified hash>
Build-Date: <UTC date>
```

Этого достаточно, чтобы видеть фактически использованный image и его checksum без отдельной системы version lock.

## Проверка после изменения builder

После существенного изменения `create-template.sh`:

1. clean build VMID 9000;
2. Full Clone;
3. noVNC/tty1 и serial fallback;
4. regular kernel + framebuffer;
5. QGA;
6. locked passwords / SSH key-only;
7. unique machine-id и SSH host keys;
8. filesystem growth после resize;
9. отсутствие builder artifacts.

CI проверяет shell/Cloud-Init структуру, но не заменяет этот PVE integration test.

## История

- **v2** — базовый build/Full Clone, QGA, SSH, timesync, TRIM, cleanup и disk growth.
- **v3** — тестировалась serial-only Web Console; признана менее удобной.
- **v4** — возвращён `vga: std`, установлен regular amd64 kernel, подтверждены framebuffer/noVNC и `tty1` autologin; текущая действующая версия.
- **v5** — эксперимент с pinned Debian build и APT snapshot; отменён как избыточно сложный для текущего проекта.
