# Базовый template Debian 13 для Proxmox

## Статус

Текущий baseline:

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 5
Clone policy: Full Clone
Protection: 1
```

Версия 5 сохраняет проверенные решения v4 по console/kernel и добавляет воспроизводимость build inputs.

## Source of truth и переходное состояние

Целевая архитектура:

```text
PUBLIC proxmox-bootstrap
└── init-pve.sh

PRIVATE proxmox
└── scripts/pve/create-template.sh
```

Private builder ещё не перенесён. Текущая рабочая **переходная** реализация Template v5 находится в public `zsergeyru/proxmox-bootstrap/create-template.sh`.

Проверенный public code baseline:

```text
ef0e3f21532c6e0fe19b79ea83f5c9b8d420d1f2
```

Нельзя считать `main` recovery reference.

Политика воспроизводимости: [`../../docs/24-reproducible-bootstrap.md`](../../docs/24-reproducible-bootstrap.md).

Политика сборки: [`build-policy.md`](build-policy.md).

## Что делает Template v5

```text
pinned Debian 13 cloud build
→ SHA-512 verification
→ VMID 9000 builder
→ build-time Debian snapshot
→ apt update/full-upgrade + base packages
→ regular linux-image-amd64
→ remove cloud-amd64 kernel
→ verification reboot
→ framebuffer + VGA/noVNC tty1 verification
→ QGA + SSH policy + locked-password verification
→ restore normal live Debian repositories
→ clean machine-specific state
→ standard Cloud-Init defaults
→ qm template
→ protection=1
```

## Reproducible inputs

Текущий lock:

```text
Debian cloud build: 20260601-2496
Debian APT snapshot: 20260914T000000Z
```

Image не берётся из `trixie/latest`.

`apt full-upgrade` во время build не идёт в меняющийся live archive: package universe фиксируется snapshot timestamp. Перед превращением VM в template normal Debian repositories возвращаются, поэтому рабочие clones можно обновлять обычным способом.

## Kernel и console

Сохраняется принятое решение v4:

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

Исторически на текущем PVE regular kernel дал `fb0 1280x800`; конкретное разрешение не зашивается как обязательное.

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
→ protection по guest.yaml
→ CPU/RAM/disk/network
→ ciuser=ops
→ SSH public keys
→ qm cloudinit update
→ start
→ verify QGA/SSH/health
```

Linked Clone не является штатным вариантом.

## `/etc/vm-template-info`

Template записывает provenance:

```text
Template: tpl-debian13
Template-Version: 5
Source-Image: debian-13-genericcloud-amd64-20260601-2496.qcow2
Source-Image-SHA512: <verified hash>
Debian-Cloud-Build: 20260601-2496
Build-Apt-Snapshot: 20260914T000000Z
Build-Date: <UTC date>
```

## Clean build gate

После изменения v5/pins требуется:

1. clean build VMID 9000;
2. новый Full Clone;
3. noVNC/tty1 и serial fallback;
4. regular kernel + framebuffer;
5. QGA;
6. locked passwords / SSH key-only;
7. unique machine-id и SSH host keys;
8. filesystem growth после resize;
9. отсутствие builder artifacts;
10. normal Debian repositories в sealed clone/template.

CI проверяет структуру scripts/Cloud-Init, но не заменяет этот PVE integration test.

## История

- **v2** — подтверждены базовый build/Full Clone, QGA, SSH, timesync, TRIM, cleanup и disk growth.
- **v3** — тестировалась serial-only Web Console; признана менее удобной.
- **v4** — возвращён `vga: std`, установлен regular amd64 kernel, подтверждены framebuffer/noVNC и `tty1` autologin.
- **v5** — pinned Debian cloud build + build-time APT snapshot; moving `latest` исключён из recovery build path.
