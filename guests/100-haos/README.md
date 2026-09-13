# 100 — HAOS (production)

VM `100` — фактически работающий Home Assistant OS, перенесённый на Proxmox с Raspberry Pi.

Это текущая production-система Home Assistant. Она сохраняет VMID `100`: существующий рабочий гость не перенумеровывается и не мигрирует только ради новой схемы VMID.

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

`100 HAOS` остаётся основным рабочим Home Assistant без заранее установленного срока вывода из эксплуатации.

`201-ha-main` является только резервом под возможную будущую миграцию на другую модель размещения Home Assistant. Само наличие VMID `201` в плане не является причиной создавать новый гость или переносить текущую систему.

Переход с `100` на `201` допускается только при появлении практической причины и отдельного плана миграции с backup, проверкой и возможностью отката.

## Сеть

VM подключена к `vmbr0`. IP внутри HAOS в host inventory не фиксировался, поэтому он не записывается здесь наугад.

Текущая сеть Proxmox-хоста описана в [`../../host/pve/network/README.md`](../../host/pve/network/README.md).

## Backup

Перед любыми изменениями текущего production Home Assistant сохранять возможность восстановления. Snapshot `good_restore` полезен для быстрого отката, но не заменяет полноценный внешний backup.
