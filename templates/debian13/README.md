# Базовый template Debian 13 для Proxmox

## Статус

Текущий baseline:

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 6
Clone policy: Full Clone
Protection: 1
Management user: root
SSH: public key only
```

Template v6 является текущей действующей версией. Версия v5 ранее использовалась для отменённого эксперимента с pinned Debian build/APT snapshot и не переиспользуется.

Рабочая реализация находится в приватном репозитории:

```text
scripts/pve/create-template.sh
```

Политика сборки: [`build-policy.md`](build-policy.md).

## Что делает Template v6

```text
Debian 13 trixie/latest generic cloud image
→ SHA-512 verification
→ VMID 9000 builder
→ apt update/full-upgrade из обычных Debian repositories
→ установка base packages
→ regular linux-image-amd64
→ remove cloud-amd64 kernel
→ root password locked
→ root SSH key-only
→ verification reboot
→ framebuffer + VGA/noVNC tty1 verification
→ QGA + SSH policy verification
→ clean machine-specific state и authorized_keys
→ standard Cloud-Init defaults
→ ciuser=root
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
noVNC/VGA → tty1 → autologin root
serial0 → ttyS0 → autologin root (fallback)
Fixed 8x16
```

Builder после reboot требует:

- kernel `*-amd64` без `cloud`;
- существующий framebuffer `fb0` с валидным размером;
- active QEMU Guest Agent;
- active `getty@tty1` и `serial-getty@ttyS0`;
- root autologin на обеих локальных консолях;
- корректную effective SSH policy.

Console autologin не является сетевой password authentication. Право Proxmox `VM.Console` фактически даёт root-доступ внутрь гостя и выдаётся только доверенным PVE identities.

## Доступ

```text
management user: root
root password: locked
PasswordAuthentication: no
KbdInteractiveAuthentication: no
PermitRootLogin: prohibit-password
PubkeyAuthentication: yes
```

То есть root по сети доступен только с разрешённым private key.

Base template не содержит personal, deployer, AI или Ansible public keys. Перед seal удаляется `/root/.ssh`, а каждый clone получает необходимые public keys отдельно до первого запуска.

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
ciuser: root
ciupgrade: 0
Timezone: Europe/Moscow
Locale: en_US.UTF-8
```

Docker и application services в base template не устанавливаются.

## Clone lifecycle

```text
Full Clone from 9000
→ CPU/RAM/disk/network
→ ciuser=root
→ установить один или несколько SSH public keys
→ qm cloudinit update
→ start
→ verify QGA/SSH/health
```

Для host-side deployer используется `pve_guest_ed25519.pub`. AI и Ansible используют собственные независимые public keys. Несколько ключей могут давать доступ одному Linux-пользователю `root` и при этом независимо отзываться удалением соответствующей строки из `/root/.ssh/authorized_keys`.

Linked Clone не является штатным вариантом.

## Идентификация версии и contract

Кроме `/etc/vm-template-info`, Proxmox description содержит машинно-читаемый marker:

```text
template-version=6
```

PVE Configuration проверяет существующий VMID 9000 по полному host-visible contract:

```text
name = tpl-debian13
template = 1
protection = 1 после ensure_template
agent = 1
vga = std
serial0 = socket
ciuser = root
ciupgrade = 0
ipconfig0 = ip=dhcp
cicustom отсутствует
description содержит template-version=6
```

До snapshot допускается единственное автоматически исправляемое отклонение: отсутствие `protection=1` у уже совместимого template. Остальные несовпадения вызывают STOP и требуют осознанной пересборки.

Старый template другой версии автоматически не изменяется и не удаляется.

## `/etc/vm-template-info`

Template v6 записывает, в частности:

```text
Шаблон: tpl-debian13
Версия-шаблона: 6
ОС: Debian 13
Тип-ядра: amd64
Management-user: root
SSH: root key-only
```

Также сохраняются source image, SHA-512 и дата сборки.

## Проверка после изменения builder

После существенного изменения `create-template.sh`:

1. clean build VMID 9000;
2. Full Clone;
3. noVNC/tty1 и serial fallback;
4. regular kernel + framebuffer;
5. QGA;
6. locked root password / SSH key-only;
7. root SSH с injected public key;
8. unique machine-id и SSH host keys;
9. filesystem growth после resize;
10. отсутствие builder artifacts и baked-in authorized_keys.

CI проверяет shell-синтаксис и структуру Cloud-Init, но не заменяет реальный PVE integration test.

## История

- **v2** — базовый build/Full Clone, QGA, SSH, timesync, TRIM, cleanup и disk growth.
- **v3** — тестировалась serial-only Web Console; признана менее удобной.
- **v4** — `ops + NOPASSWD sudo`, VGA/noVNC и regular amd64 kernel; прежний baseline.
- **v5** — эксперимент с pinned Debian build и APT snapshot; отменён и номер не переиспользуется.
- **v6** — единый management user `root`, пароль root locked, root SSH только по ключу, отдельные SSH identities различаются ключами.
