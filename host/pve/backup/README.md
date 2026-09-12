# Host backup

## Фактическое состояние на 2026-09-12

На Proxmox уже существует storage:

```text
name: backup
type: dir
path: /mnt/pve/backup
content: backup
```

Физический носитель — USB Kingston DataTraveler около `58 GiB`, `ext4`.

На момент снимка:

```text
total: ~56.5 GiB
used: ~19.7 GiB
available: ~33.9 GiB
```

Таким образом backup storage уже фактически используется, но это пока не считается законченной постоянной backup-архитектурой.

## Что уже есть

- отдельный Proxmox storage `backup`;
- USB-носитель отделён от основного NVMe;
- у VM 100 существует snapshot `good_restore`;
- ранее создавался полноценный backup HAOS VM 100.

Snapshot не считается заменой полноценному backup.

## Что нужно проверить

В снятом `/etc/fstab` отсутствовала запись для USB-раздела `/mnt/pve/backup`. Нужно отдельно выяснить, каким механизмом накопитель монтируется после загрузки и гарантированно ли storage возвращается после reboot.

До проверки нельзя считать automount USB backup полностью задокументированным и воспроизводимым.

Также остаются открытыми решения:

- расписание backup;
- retention;
- какие гости и persistent data копируются обязательно;
- проверка восстановления из backup;
- нужен ли позднее отдельный Proxmox Backup Server;
- постоянный ли USB-накопитель останется backup-хранилищем или станет временным.

Общая backup-политика проекта находится в [`../../../docs/storage-and-backup.md`](../../../docs/storage-and-backup.md).
