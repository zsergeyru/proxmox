# 100 — HAOS (legacy/current)

VM `100` — фактически работающий Home Assistant OS, перенесённый на Proxmox с Raspberry Pi.

Это текущая рабочая система Home Assistant, но она не является частью новой целевой VMID-схемы. Поэтому она сохраняет VMID `100` как legacy-гость до отдельного решения о миграции.

## Фактическое состояние на 2026-09-12

```text
Proxmox name: HAOS
state: running
VMID: 100
vCPU: 2
RAM: 4096 MiB
balloon minimum: 2048 MiB
system disk: 32 GiB
BIOS: OVMF/UEFI
QEMU Guest Agent: enabled
onboot: enabled
network bridge: vmbr0
Proxmox firewall on net0: enabled
```

CPU type:

```text
x86-64-v2-AES
```

Storage:

```text
EFI: local-lvm, 4 MiB
system disk: local-lvm, 32 GiB
controller: virtio-scsi-single
```

Существует snapshot/parent state:

```text
good_restore
```

## Роль в архитектуре

`100 HAOS` остаётся рабочим Home Assistant на переходный период.

Целевая новая схема содержит `201-ha-main`, однако наличие `201` не означает, что VM 100 нужно немедленно удалять или перенумеровывать. Миграция выполняется только отдельным проверенным этапом с backup и возможностью отката.

## Сеть

VM подключена к `vmbr0`. IP внутри HAOS в host inventory не фиксировался, поэтому он не записывается здесь наугад.

Текущая сеть Proxmox-хоста описана в [`../../host/pve/network/README.md`](../../host/pve/network/README.md).

## Backup

Перед любыми изменениями текущего production Home Assistant сохранять возможность восстановления. Snapshot `good_restore` полезен для быстрого отката, но не заменяет полноценный внешний backup.
