# Новая структура блока 20 — PVE host

> **Статус:** проект реорганизации документации.
>
> Старые файлы `docs/20-*.md` … `docs/29-*.md` пока остаются действующими и не удаляются.
> Эта папка используется для последовательной подготовки нового блока документации.
> После полного переноса содержания, проверки перекрёстных ссылок и отсутствия потерь старые файлы будут удалены отдельным изменением.

## Цель

Блок `20` должен описывать сам Proxmox VE host и инфраструктуру его управления.

Главный принцип новой структуры:

- один файл — одна основная ответственность;
- `20-overview.md` — входная точка и карта всего блока;
- подробности не дублируются между файлами;
- обзорные файлы ссылаются на профильные спецификации и инструкции;
- текущий статус реализации хранится отдельно от архитектурных требований;
- guest lifecycle, `guest.yaml`, `deploy-guest` и Guest Bootstrap остаются в блоке `30`.

## Предлагаемые файлы

### `20-overview.md` — PVE host: обзор блока

Главная входная точка раздела `20`.

Отвечает на вопросы:

- какую роль выполняет PVE host;
- из каких частей состоит host-side management;
- что такое Public Bootstrap и PVE Configuration;
- какие существуют системные пользователи и технические identities;
- где находятся границы между PVE host, deploy-контуром и guest lifecycle;
- где искать подробности по initialization, filesystem, security, access, backup и recovery.

Документ должен быть коротким и не дублировать профильные спецификации.

### `21-initialization.md` — инициализация PVE host

Практическая инструкция оператора:

- предварительные условия;
- первый запуск;
- повторный запуск;
- продолжение после ошибки;
- полное обновление;
- проверка результата;
- проверка template `9000`;
- диагностический запуск PVE Configuration.

Основа: текущий `docs/20-pve-initialization.md`.

### `22-host-filesystem.md` — файловая структура PVE management

Справочник:

- каталоги Public Bootstrap;
- trusted checkout PVE Configuration;
- runtime/state;
- lock files;
- конфигурация deploy-контура;
- secrets и credentials;
- владельцы, группы и permissions;
- какие каталоги mutable, а какие считаются доверенным кодом.

Основа: текущий `docs/21-pve-filesystem-layout.md`.

### `23-reproducibility.md` — версии, доверие и воспроизводимость

Политика:

- версии Public Bootstrap;
- версия PVE Configuration;
- версия template;
- одна Git revision на один запуск;
- trusted checkout;
- root trust boundary;
- идемпотентность;
- сохранность state;
- работа с одноразовыми secret values;
- требования CI и проверок.

Основа: текущий `docs/24-reproducible-bootstrap.md`.

### `24-security.md` — общая безопасность PVE и management-контура

Только верхнеуровневые правила безопасности:

- принцип наименьших прав;
- разделение host / PVE API / guest OS;
- правила хранения secrets;
- SSH только по ключам;
- запрет автоматического расширения собственных прав;
- требования к destructive operations;
- защита template и control-plane объектов;
- требования к журналированию.

Подробный lifecycle management SSH keys сюда не переносится — он должен жить в отдельной спецификации.

Основа: общие правила из текущего `docs/23-security.md`.

### `25-access-control.md` — пользователи, роли и ACL Proxmox

Точная спецификация PVE access control:

- пользователи и API tokens;
- custom roles;
- privileges;
- ACL;
- resource pool `managed`;
- protected objects;
- права deployer;
- права AI;
- повторное применение ACL и token configuration.

Основа: текущий `docs/25-pve-access-control.md`.

### `26-management-identities.md` — технические identities и management SSH

Точная спецификация инфраструктурных identities:

- deployer SSH identity;
- management identity управляющего guest;
- public-key registry;
- `management-authorized-keys`;
- `management-ssh`;
- `sync-management-keys`;
- lifecycle, регистрация, ротация и отзыв;
- граница SSH identity и Git credential;
- восстановление identities как концепция, без аварийного runbook.

Основа: текущий `docs/28-management-ssh-keys.md` и профильные части текущего `docs/23-security.md`.

### `27-storage-and-backup.md` — хранение и политика backup

Политика:

- схема storage;
- классы backup;
- критичные и воспроизводимые гости;
- RPO/RTO;
- persistent data;
- backup credentials и management identities;
- внешние storage;
- требования к регулярной проверке восстановления.

Основа: текущий `docs/22-storage-and-backup.md`.

### `28-disaster-recovery.md` — аварийное восстановление

Runbook:

- проверка восстановления одной VM/LXC;
- потеря системного диска PVE;
- порядок восстановления PVE;
- подключение backup storage;
- восстановление credentials и identities;
- восстановление public-key registry;
- порядок запуска гостей;
- действия после восстановления.

Основа: текущий `docs/27-backup-and-disaster-recovery-runbook.md`.

### `29-implementation-status.md` — статус реализации host-side контрактов

Справочник только о фактическом состоянии реализации:

- реализовано;
- реализовано частично;
- принято, но ещё не реализовано;
- способ проверки.

Не содержит roadmap, архитектурных решений и инструкций.

Основа: текущий `docs/29-implementation-status.md`, но только для контрактов блока `20`.

## Что делать с текущим обзором доступа

Текущий `docs/26-deploy-guest-and-agent-access.md` полезен как объяснение для человека, но частично пересекается с access control, management SSH и guest deploy.

При миграции его содержание нужно разобрать:

- host/PVE access model → `20-overview.md` и `25-access-control.md`;
- management identities → `26-management-identities.md`;
- создание и управление гостями → блок `30`;
- повторяющиеся объяснения удалить после появления однозначных ссылок.

Отдельный файл-обзор для этой темы, вероятно, больше не потребуется.

## Предварительная карта миграции

| Старый файл | Новый владелец содержания |
|---|---|
| `20-pve-initialization.md` | `21-initialization.md` |
| `21-pve-filesystem-layout.md` | `22-host-filesystem.md` |
| `22-storage-and-backup.md` | `27-storage-and-backup.md` |
| `23-security.md` | `24-security.md` + `26-management-identities.md` |
| `24-reproducible-bootstrap.md` | `23-reproducibility.md` |
| `25-pve-access-control.md` | `25-access-control.md` |
| `26-deploy-guest-and-agent-access.md` | разобрать между `20`, `25`, `26` и блоком `30` |
| `27-backup-and-disaster-recovery-runbook.md` | `28-disaster-recovery.md` |
| `28-management-ssh-keys.md` | `26-management-identities.md` |
| `29-implementation-status.md` | `29-implementation-status.md` |

## Порядок дальнейшей работы

1. Сначала заполнить `20-overview.md` и зафиксировать границы блока.
2. По одному переносить профильные документы, не копируя дублирование.
3. После каждого переноса проверять, что новая версия покрывает всё значимое содержание старого файла.
4. Проверить ссылки из остальных документов репозитория.
5. Обновить `docs/README.md`.
6. Только после полной проверки удалить старые `docs/20-*.md` … `docs/29-*.md`.
