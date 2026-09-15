# Templates

Шаблоны конфигураций, VM/LXC и повторяемых заготовок. Шаблон не должен содержать реальные секреты, токены, пароли или приватные ключи.

## VM templates

- [`debian13/`](./debian13/) — базовый универсальный шаблон Debian 13 для Proxmox: параметры VM, состав системных утилит, backup-инструменты и границы ответственности template.
- [`debian13/build-policy.md`](./debian13/build-policy.md) — согласованная политика сборки без `virt-customize`, базовые настройки ОС, root-only key-based SSH, `Europe/Moscow`, диск/TRIM/iothread, console, обновления, очистка и versioning.
- [`debian13/filesystem-layout.md`](./debian13/filesystem-layout.md) — стандарт размещения кода, конфигурации, persistent state, пользовательских данных, логов, cache и runtime-файлов (`/opt`, `/etc`, `/var/lib`, `/srv`, `/var/log`, `/var/cache`, `/run`, `/tmp`) с правилами backup и восстановления.
- [`debian13/users-and-keys.md`](./debian13/users-and-keys.md) — root management model, независимые технические SSH identities, Cloud-Init и правила backup private technical keys.
