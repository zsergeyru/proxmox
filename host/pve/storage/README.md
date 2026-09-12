# Host storage

## Фактическое состояние на 2026-09-12

Основной накопитель:

```text
NVMe: 512 GB nominal / 476.9 GiB visible
model: SSD 512GB
```

Разметка:

```text
EFI        1 GiB
pve-root   48 GiB ext4
pve-swap   8 GiB
pve-data   ~395.9 GiB LVM-thin
```

Volume group:

```text
VG: pve
size: ~475.94 GiB
free: 16 GiB
```

LVM-thin pool `data` на момент снимка занят примерно на `7.63%`.

## Proxmox storage

```text
local
  type: dir
  path: /var/lib/vz
  content: import,vztmpl,iso

local-lvm
  type: lvmthin
  vg: pve
  thinpool: data
  content: images,rootdir

backup
  type: dir
  path: /mnt/pve/backup
  content: backup
```

Состояние на момент снимка:

```text
local       ~46.9 GiB total, ~6.5 GiB used
local-lvm   ~395.9 GiB total, ~30.2 GiB allocated/used by thin volumes
backup      ~56.5 GiB total, ~19.7 GiB used, ~33.9 GiB available
```

## Текущие виртуальные диски

```text
VM 100 HAOS
  EFI disk: 4 MiB
  system disk: 32 GiB
  snapshot/state good_restore exists

VM 320 ai-agent
  system disk: 16 GiB
```

## USB backup storage

Физически storage `backup` находится на USB Kingston DataTraveler объёмом около `58 GiB`, файловая система `ext4`, mount point:

```text
/mnt/pve/backup
```

Способ гарантированного монтирования после перезагрузки ещё нужно проверить: в снятом `/etc/fstab` записи для этого USB-раздела не было.

Backup-политика и этот открытый вопрос описаны в [`../backup/README.md`](../backup/README.md).
