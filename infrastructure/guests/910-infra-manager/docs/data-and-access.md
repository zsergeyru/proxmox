# Данные и доступы 910 infra-manager

**Назначение:** практическое описание каталогов, подключений, прав и временных файлов, без которых службы 910 не смогут развернуться.

Этот файл отвечает на вопрос: **что должно быть подключено к 910, где это видно внутри гостя и как проверить, что службы работают с правильными данными**.

## 1. Общая схема

На PVE используется единый корень:

```text
/mnt/bindmounts/infra-manager/
├── pve-only/
├── access/
└── state/
```

Внутрь 910 подключаются только две области:

```text
access/  → /mnt/pve-access       RO
state/   → /mnt/persistent-state RW
```

`pve-only/` внутрь LXC не подключается.

Это значение задаётся в `provision.yaml:persistence.target_layout`.

## 2. Область pve-only

Физический путь:

```text
/mnt/bindmounts/infra-manager/pve-only/
```

Эта область нужна PVE для операций, которые нельзя доверять обычной файловой системе 910.

В частности, OpenBao использует:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao/unseal.key
/mnt/bindmounts/infra-manager/pve-only/openbao/ssh-access.json
/mnt/bindmounts/infra-manager/pve-only/openbao/kv-access.json
```

Также PVE-only область содержит аварийную read-only копию Git credential.

Эти файлы не должны появляться как постоянные файлы внутри 910.

Проверять их нужно с PVE, а не из LXC.

## 3. Область access

Физический путь PVE:

```text
/mnt/bindmounts/infra-manager/access
```

В 910:

```text
/mnt/pve-access
```

Режим подключения:

```text
read-only
```

### 3.1. Доступ root SSH к PVE

910 ожидает:

```text
/mnt/pve-access/pve-host/root_ed25519
/mnt/pve-access/pve-host/known_hosts
```

Ansible проверяет закрытый ключ и устанавливает для него владельца `1001:0` и режим `0600` в доступном bind mount.

`known_hosts` получает `1001:0`, режим `0644`.

Эта идентичность используется служебными командами 910, которым требуется действие непосредственно на PVE.

### 3.2. CA PVE

Ожидаемый исходный файл:

```text
/mnt/pve-access/ca/pve-root-ca.crt
```

Ansible:

1. проверяет, что файл существует и не пуст;
2. копирует его в `/usr/local/share/ca-certificates/pve-root-ca.crt`;
3. выполняет `update-ca-certificates`;
4. собирает общий набор:

```text
/etc/infra-manager/ca/ca-bundle.crt
```

Проверить:

```bash
test -s /mnt/pve-access/ca/pve-root-ca.crt
test -s /usr/local/share/ca-certificates/pve-root-ca.crt
test -s /etc/infra-manager/ca/ca-bundle.crt
```

## 4. Область state

Физический путь PVE:

```text
/mnt/bindmounts/infra-manager/state
```

В 910:

```text
/mnt/persistent-state
```

Режим:

```text
read-write
```

Основные каталоги:

| Путь | Владелец | Режим | Назначение |
|---|---:|---:|---|
| `/mnt/persistent-state/ansible` | `1001:0` | `0700` | переходная SSH-идентичность Ansible |
| `/mnt/persistent-state/semaphore` | `1001:0` | `0770` | SQLite, история и данные Semaphore |
| `/mnt/persistent-state/opentofu` | `1001:0` | `0750` | входы и состояние OpenTofu |
| `/mnt/persistent-state/openbao` | `root:root` | `0700` | Raft OpenBao |

Точные значения находятся в `provision.yaml:persistence.target_layout.state.bindings`.

## 5. Как Ansible готовит постоянные каталоги

Реализация:

```text
automation/ansible/roles/infra_manager/tasks/persistence.yml
```

### 5.1. Сначала проверяются bind mount

Настройка начинается с:

```bash
mountpoint -q /mnt/pve-access
mountpoint -q /mnt/persistent-state
```

Если подключение отсутствует, роль не создаёт локальный каталог вместо него.

### 5.2. Проверяются старые пути

Перед созданием ссылок Ansible проверяет старые локальные пути.

Если ожидаемый путь:

- уже является правильной ссылкой — он остаётся;
- является пустым каталогом — его можно заменить ссылкой;
- содержит неизвестные данные — настройка останавливается и требует ручной миграции;
- является ссылкой на неожиданный источник — настройка останавливается.

Это защищает от тихой потери старого состояния.

### 5.3. Создаются рабочие ссылки

После проверки создаются:

```text
/etc/infra-manager/ansible
    → /mnt/persistent-state/ansible

/var/lib/infra-manager/semaphore
    → /mnt/persistent-state/semaphore

/var/lib/infra-manager/opentofu
    → /mnt/persistent-state/opentofu

/var/lib/persistent/openbao
    → /mnt/persistent-state/openbao
```

Проверить:

```bash
readlink -f /etc/infra-manager/ansible
readlink -f /var/lib/infra-manager/semaphore
readlink -f /var/lib/infra-manager/opentofu
readlink -f /var/lib/persistent/openbao
```

## 6. OpenTofu state

Внутри постоянного каталога создаётся:

```text
/mnt/persistent-state/opentofu/state/
```

Владелец:

```text
1001:0
```

Режим:

```text
0750
```

Основной state:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Он физически находится в `state/opentofu/state/` на PVE.

## 7. Постоянная идентичность Ansible

Текущая переходная пара хранится в:

```text
/mnt/persistent-state/ansible/guest_ed25519
/mnt/persistent-state/ansible/guest_ed25519.pub
```

Если закрытого ключа нет, `ansible_access.yml` создаёт Ed25519-пару.

Закрытый ключ:

```text
owner 1001:0
mode 0600
```

Открытый:

```text
owner 1001:0
mode 0644
```

Открытая часть также добавляется в `/root/.ssh/authorized_keys` самого 910, чтобы общий Ansible-механизм мог управлять 910.

Эта пара является переходной и не должна автоматически считаться целевой схемой SSH.

## 8. Временная область рабочих секретов

Ansible создаёт:

```text
/run/infra-manager/secrets
```

с:

```text
owner root:root
mode 0750
```

После создания OpenBao там появляются рабочие файлы:

```text
pve-api.env
github_proxmox_repo_ed25519
semaphore-server.env
initial-admin-password
semaphore-api-token
.openbao-materialized
```

Часть файлов появляется только после соответствующей операции, например API-токен Semaphore — после настройки проекта.

`infra-runtime` получает весь этот каталог только для чтения.

## 9. Первый запуск до готовности OpenBao

При самом первом создании 910 bootstrap может передать:

```text
/run/infra-manager/bootstrap-secrets/pve-api.env
/run/infra-manager/bootstrap-secrets/github_proxmox_repo_ed25519
```

Если OpenBao ещё не может восстановить рабочие значения, Ansible копирует эти staging-файлы в рабочую `/run/infra-manager/secrets/` с владельцем `1001:0` и режимом `0600`.

После инициализации OpenBao постоянным источником становятся его KV-секреты, а bootstrap-файлы больше не используются как штатный источник.

## 10. Как восстанавливаются рабочие секреты

Во время повторной настройки `pve_access.yml` сначала пытается вызвать на PVE:

```text
/usr/local/sbin/infra-manager-openbao-unseal
```

через отдельный root SSH канал.

Если существующий OpenBao доступен, PVE:

- разблокирует его при необходимости;
- читает ограниченные KV через PVE-only AppRole;
- создаёт рабочие файлы в `/run/infra-manager/secrets/`.

После этого Ansible проверяет наличие `pve-api.env` и Git credential.

Если рабочие значения не восстановлены и bootstrap-значений тоже нет, настройка останавливается.

## 11. Воспроизводимые локальные данные

Следующие данные находятся в корневой файловой системе 910, но могут быть созданы заново:

```text
/var/lib/infra-manager/bootstrap-repo/
/opt/infra-manager/compose/
/usr/local/lib/infra-manager/
/usr/local/sbin/infra-manager-*
/etc/infra-manager/status.yaml
Docker images
кэши
```

Их потеря не должна требовать восстановления резервной копии, если сохранены Git и постоянное состояние.

## 12. Что проверять после развёртывания

Проверить mount:

```bash
mountpoint /mnt/pve-access
mountpoint /mnt/persistent-state
```

Проверить постоянные каталоги:

```bash
stat /mnt/persistent-state/ansible
stat /mnt/persistent-state/semaphore
stat /mnt/persistent-state/opentofu
stat /mnt/persistent-state/openbao
```

Проверить ссылки:

```bash
readlink -f /etc/infra-manager/ansible
readlink -f /var/lib/infra-manager/semaphore
readlink -f /var/lib/infra-manager/opentofu
readlink -f /var/lib/persistent/openbao
```

Проверить PVE-доступ:

```bash
test -s /mnt/pve-access/pve-host/root_ed25519
test -s /mnt/pve-access/pve-host/known_hosts
test -s /mnt/pve-access/ca/pve-root-ca.crt
```

Проверить рабочие секреты:

```bash
ls -la /run/infra-manager/secrets/
```

Полная проверка:

```bash
infra-manager-status --full
```

## 13. Что нужно сохранить для восстановления 910

Локальная карта данных:

| Данные | Где находятся | Можно создать заново |
|---|---|---|
| OpenBao Raft | `state/openbao` | нет |
| unseal key и PVE-only AppRole | `pve-only/openbao` | не как обычное восстановление |
| Semaphore | `state/semaphore` | пустую базу — да, прежнее состояние — нет |
| OpenTofu state | `state/opentofu/state` | не поверх существующей инфраструктуры |
| переходный Ansible key | `state/ansible` | технически можно перевыпустить, но требуется согласованная ротация |
| PVE root SSH / CA | `access/` | только через отдельную процедуру выдачи |
| Compose, Python, команды | Git / rootfs | да |
| Docker images | локальный Docker | да |
| `/run/infra-manager/secrets` | временно | да, из OpenBao |

Общая политика резервирования остаётся в [`610-backup.md`](../../../../docs/600-storage/610-backup.md).

## 14. Восстановление каталогов после пересоздания LXC

Правильный порядок:

1. на PVE убедиться, что сохранены `pve-only`, `access`, `state`;
2. подключить `access` к `/mnt/pve-access` как RO;
3. подключить `state` к `/mnt/persistent-state` как RW;
4. не создавать пустые локальные замены ожидаемых bind mount;
5. запустить общую Ansible-настройку 910;
6. дать `persistence.yml` проверить старые пути и создать рабочие ссылки;
7. восстановить рабочие секреты через существующий OpenBao;
8. собрать контейнеры и выполнить проверки сервисов;
9. завершить `infra-manager-status --full`.

Если Ansible сообщает, что старый путь содержит неизвестные данные, сначала нужно разобраться с этими данными вручную. Удалять их только ради прохождения роли нельзя.

## 15. Типичные неисправности

### 15.1. `mountpoint` сообщает ошибку

Причина находится вне служб 910: ожидаемый bind mount не подключён PVE.

Не нужно создавать `/mnt/persistent-state` или `/mnt/pve-access` как обычный локальный каталог и продолжать установку.

### 15.2. Ansible отказывается заменить старый каталог ссылкой

Проверить:

```bash
ls -la <путь>
readlink -f <путь>
```

Если там сохранилось старое состояние, его нужно осознанно мигрировать в соответствующий каталог `/mnt/persistent-state`.

### 15.3. Нет `pve-api.env`

Проверить:

```bash
systemctl status infra-manager-openbao-startup-unseal.service
ls -la /run/infra-manager/secrets/
```

На первом запуске дополнительно проверить staging-файл `/run/infra-manager/bootstrap-secrets/pve-api.env`.

### 15.4. `infra-runtime` не может писать SQLite или OpenTofu state

Проверить числовых владельцев:

```bash
stat -c '%u:%g %a %n' /mnt/persistent-state/semaphore
stat -c '%u:%g %a %n' /mnt/persistent-state/opentofu
```

Ожидаемый рабочий пользователь контейнера — `1001:0`.

## 16. Связанные документы

- [`../provision.yaml`](../provision.yaml) — машинное описание всех путей и bindings.
- [`openbao.md`](openbao.md) — Raft, PVE-only данные и материализация секретов.
- [`semaphore.md`](semaphore.md) — постоянная база Semaphore.
- [`infra-runtime.md`](infra-runtime.md) — подключения контейнера.
- [`automation-tools.md`](automation-tools.md) — OpenTofu state и Ansible.
- [`../../../../automation/ansible/roles/infra_manager/tasks/persistence.yml`](../../../../automation/ansible/roles/infra_manager/tasks/persistence.yml) — создание каталогов и ссылок.
- [`../../../../automation/ansible/roles/infra_manager/tasks/pve_access.yml`](../../../../automation/ansible/roles/infra_manager/tasks/pve_access.yml) — CA PVE и восстановление рабочих секретов.
- [`../../../../automation/ansible/roles/infra_manager/tasks/ansible_access.yml`](../../../../automation/ansible/roles/infra_manager/tasks/ansible_access.yml) — переходная SSH-идентичность Ansible.
- [`../../../../docs/200-pve/230-host-layout.md`](../../../../docs/200-pve/230-host-layout.md) — общая структура PVE.
- [`../../../../docs/600-storage/610-backup.md`](../../../../docs/600-storage/610-backup.md) — политика резервирования.