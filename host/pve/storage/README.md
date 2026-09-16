# Хранилища PVE-хоста

## Фактическое состояние на 2026-09-12

Основной накопитель:

```text
NVMe: 512 GB nominal / 476.9 GiB visible
model: SSD 512GB
```

Технические поля в блоках оставлены в виде, близком к фактическому выводу системы.

Разметка:

```text
EFI        1 GiB
pve-root   48 GiB ext4
pve-swap   8 GiB
pve-data   ~395.9 GiB LVM-thin
```

Группа томов:

```text
VG: pve
size: ~475.94 GiB
free: 16 GiB
```

Тонкий пул LVM `data` на момент снимка занят примерно на `7.63%`.

## Хранилища Proxmox

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

Имена параметров Proxmox оставлены без перевода.

Состояние на момент снимка:

```text
local       ~46.9 GiB всего, ~6.5 GiB занято
local-lvm   ~395.9 GiB всего, ~30.2 GiB выделено тонким томам
backup      ~56.5 GiB всего, ~19.7 GiB занято, ~33.9 GiB свободно
```

## Текущие виртуальные диски

```text
VM 100 HAOS
  EFI-диск: 4 MiB
  системный диск: 32 GiB
  существует снимок/состояние good_restore

VM 320 ai-agent
  системный диск: 16 GiB
```

## USB-хранилище резервных копий

Физически хранилище `backup` находится на USB-накопителе Kingston DataTraveler объёмом около `58 GiB`, файловая система `ext4`, точка монтирования:

```text
/mnt/pve/backup
```

Способ гарантированного монтирования после перезагрузки ещё нужно проверить: в снятом `/etc/fstab` записи для этого USB-раздела не было.

Политика резервного копирования и этот открытый вопрос описаны в [`../backup/README.md`](../backup/README.md).
