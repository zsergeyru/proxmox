# Semaphore на 910 infra-manager

**Тип:** локальная спецификация службы
**Статус:** целевое состояние
**Назначение:** полностью описать, каким должен быть Semaphore на 910, чтобы по этому документу можно было создать, настроить, проверить, обновить и восстановить службу.

Semaphore является интерфейсом запуска инфраструктурных заданий. Он не получает дополнительные права сам по себе: PVE, Git, OpenBao и SSH-доступы определяются профильными контрактами проекта.

## 1. Состав и зависимости

### Размещение

Semaphore должен работать внутри контейнера:

```text
infra-runtime
```

Отдельный контейнер Semaphore не нужен.

Базовый образ `infra-runtime`:

```text
semaphoreui/semaphore:<version>
```

Точная версия задаётся в [`../provision.yaml`](../provision.yaml).

Контейнер использует:

```text
network_mode: host
```

Semaphore должен слушать:

```text
0.0.0.0:3000
```

Проверка внутри 910:

```text
http://127.0.0.1:3000/api/ping
```

### Зависимости

До запуска должны быть готовы:

1. Docker;
2. `/mnt/persistent-state/semaphore`;
3. OpenBao либо bootstrap-секреты первого запуска;
4. `/run/infra-manager/secrets/semaphore-server.env`;
5. `/run/infra-manager/secrets/initial-admin-password`;
6. read-only Git credential;
7. PVE API credential;
8. собранный `infra-runtime`.

Если обязательный secret отсутствует, Semaphore не должен запускаться с автоматически придуманным вторым источником настроек.

## 2. Данные и секреты

### Постоянная база

Semaphore должен использовать SQLite:

```text
/var/lib/semaphore/semaphore.sqlite
```

Compose подключает:

```text
/mnt/persistent-state/semaphore
        ↓
/var/lib/semaphore
```

Рабочая ссылка 910:

```text
/var/lib/infra-manager/semaphore
→ /mnt/persistent-state/semaphore
```

Целевые права:

```text
owner: 1001:0
directories: 0770
files:       0660
```

В постоянном каталоге находятся:

- SQLite;
- история заданий;
- настройки Semaphore;
- Git `known_hosts`;
- признак безопасного самообновления `infra-runtime`.

### Серверные секреты

Рабочий файл:

```text
/run/infra-manager/secrets/semaphore-server.env
```

Должен содержать:

```text
SEMAPHORE_DB_DIALECT=sqlite
SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite
SEMAPHORE_ADMIN=admin
SEMAPHORE_ADMIN_PASSWORD=<secret>
SEMAPHORE_ADMIN_NAME=Admin
SEMAPHORE_ADMIN_EMAIL=admin@localhost
SEMAPHORE_ACCESS_KEY_ENCRYPTION=<secret>
TZ=<timezone>
```

Первоначальный пароль отдельно доступен:

```text
/run/infra-manager/secrets/initial-admin-password
```

После готовности OpenBao постоянным источником должны быть:

```text
infra-secrets/services/semaphore
```

с полями:

```text
admin_password
access_key_encryption
api_token
timezone
```

Файлы в `/run` являются временным представлением и восстанавливаются из OpenBao.

### API token

Рабочий файл:

```text
/run/infra-manager/secrets/semaphore-api-token
```

Если token отсутствует или перестал работать, синхронизация должна:

1. войти как `admin`;
2. создать новый token `infra-manager setup`;
3. проверить его;
4. записать временный файл с режимом `0600`;
5. сохранить новое значение обратно в OpenBao.

## 3. Первоначальная настройка и проект

### Первый запуск

Если OpenBao ещё не инициализирован, первоначальный контур может сгенерировать:

- пароль `admin`;
- `SEMAPHORE_ACCESS_KEY_ENCRYPTION`.

После переноса в OpenBao эти значения не должны иметь второй постоянный файловый источник.

Контейнер запускается через общий Compose:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d --remove-orphans
```

После запуска:

```bash
curl -fsS http://127.0.0.1:3000/api/ping
```

должен завершаться успешно.

### Синхронизация проекта

На 910 должна существовать повторяемая команда:

```bash
PYTHONPATH=/usr/local/lib/infra-manager \
python3 -m infra_manager semaphore-project
```

Она должна создавать или приводить к точному целевому состоянию:

```text
Project:       Proxmox Infrastructure
Repository:    proxmox
SSH key:       GitHub project read-only
VariableGroup: OpenTofu PVE
VariableGroup: Infra Manager
Templates:     целевой набор инфраструктурных заданий
```

Проект:

```text
max_parallel_tasks = 1
```

Числовой ID сохраняется в:

```text
/var/lib/infra-manager/semaphore-project-id
```

При нескольких объектах с одним ожидаемым именем синхронизация должна остановиться, а не выбирать один случайно.

### Git

Репозиторий:

```text
git@github.com:zsergeyru/proxmox.git
```

Credential:

```text
GitHub project read-only
```

Рабочая закрытая часть:

```text
/run/infra-manager/secrets/github_proxmox_repo_ed25519
```

SSH обязан использовать строгую проверку:

```text
StrictHostKeyChecking yes
UserKnownHostsFile /var/lib/semaphore/known_hosts
```

Ветка берётся из `INFRA_PROJECT_BRANCH`, по умолчанию `main`.

## 4. Переменные и задания

### Variable Group OpenTofu PVE

Группа должна формироваться из:

```text
/run/infra-manager/secrets/pve-api.env
```

Обязательные значения:

```text
TF_VAR_pve_endpoint
TF_VAR_pve_api_token   secret
```

SSH identity Ansible не должна храниться здесь как постоянный private key. Штатный административный SSH должен получать краткоживущий client certificate по спецификации OpenBao.

### Variable Group Infra Manager

Обязательная настройка:

```text
INFRA_LOG_LEVEL
```

Допустимые значения:

| Значение | Назначение |
|---|---|
| `normal` | обычная эксплуатация |
| `verbose` | разработка и диагностика |
| `quiet` | минимальный вывод |

Значение по умолчанию:

```text
normal
```

Повторная синхронизация должна сохранять вручную выбранное допустимое значение.

### Целевые задания

Semaphore должен содержать как минимум:

| Задание | Назначение |
|---|---|
| `OpenTofu Plan` | показать изменения инфраструктуры |
| `Build Template 9000` | собрать базовый шаблон Debian |
| `Deploy Guest` | полностью привести выбранного гостя к `guest.yaml + provision.yaml` |
| `Status Guest` | проверить выбранного гостя без изменения состояния |
| `Repair Guest` | выполнить только безопасное базовое исправление выбранного гостя |
| `Test Guest` | выполнить расширенную проверку состояния и административной сети |
| `Sync Guest` | синхронизировать данные роли без полного Deploy |
| `Sync SSH Access` | синхронизировать AppRole, policy и OTP-роли OpenBao из `access.yaml` |

Пять гостевых заданий должны использовать одну и ту же обязательную survey-переменную `GUEST_VMID` типа `enum`. Список вариантов строится автоматически только из каталогов, где одновременно существуют `guest.yaml` и `provision.yaml`. Произвольный VMID через свободные аргументы Semaphore не разрешается.

Все пять шаблонов используют общий Python-вход `scripts/infra-manager/jobs/guest-operation.py`; операция фиксируется самим шаблоном, а не выбирается вторым полем формы. Поэтому история Semaphore сразу показывает, что выполнялось: Deploy, Status, Repair, Test или Sync.

`Status` не должен менять состояние. `Repair` не является скрытым Deploy и может выполнять только безопасные повторяемые исправления. `Test` выполняет более глубокие проверки. `Sync` предназначен для данных, которые можно обновить без пересборки гостя.

Для роли `infra-manager` `Sync Guest` использует уже полученную Semaphore актуальную рабочую копию Git и повторно выполняет декларативную синхронизацию проекта Semaphore. Поэтому добавление нового гостя в Git можно отразить в выпадающих списках без `Deploy Guest` самого 910. Для остальных ролей отдельные обработчики Sync добавляются по мере появления синхронизируемых данных; неподдерживаемый Sync должен завершаться понятной ошибкой, а не превращаться в Deploy.

Первичная команда `scripts/infra-manager/jobs/initialize-openbao.py` сохраняется как внутренний механизм bootstrap/recovery и не должна отображаться как обычный шаблон Semaphore. Оператор использует только PVE-команды `infra-manager repair` и `infra-manager recover`.

Задание `Sync SSH Access` должно выполнять только идемпотентную синхронизацию OpenBao, описанную в [`openbao.md`](openbao.md). Оно не развёртывает гостей и не выполняет сквозные SSH-тесты. Настройка гостя относится к `Deploy Guest`, а приёмочная проверка выполняется отдельным сценарием `scripts/acceptance/verify-ssh-access.py`.

## 5. Жизненный цикл и самообновление

### Обычная синхронизация

После настройки 910 необходимо:

1. дождаться Semaphore;
2. проверить инструменты `infra-runtime`;
3. выполнить `semaphore-project`;
4. проверить целевой набор объектов;
5. выполнить `infra-manager-status --full --quiet`.

Та же декларативная синхронизация должна быть доступна без пересборки 910 через `Sync Guest` с выбранным гостем роли `infra-manager`. Semaphore перед запуском задания получает настроенную ветку Git, поэтому новый список гостей строится из текущей рабочей копии задания.

Синхронизация должна быть идемпотентной и удалять управляемые устаревшие объекты, если они больше не входят в целевой состав.

### Самообновление 910

При `Deploy Guest` с выбранным `910 infra-manager` текущий `infra-runtime` нельзя уничтожать до завершения выполняющегося в нём задания.

После Ansible внешняя активация должна:

1. дождаться завершения текущего процесса;
2. запустить новый Compose;
3. выполнить OpenBao startup unseal;
4. дождаться `/api/ping`;
5. синхронизировать Semaphore;
6. выполнить полный status.

Подробности находятся в [`infra-runtime.md`](infra-runtime.md).

### Обновление Semaphore

Версия меняется в `provision.yaml`.

Обновление должно:

1. пересобрать `infra-runtime`;
2. сохранить старый `/mnt/persistent-state/semaphore`;
3. сохранить тот же `SEMAPHORE_ACCESS_KEY_ENCRYPTION`;
4. активировать новый контейнер только после завершения текущего задания;
5. повторно синхронизировать проект;
6. выполнить полный status.

Ручные изменения внутри контейнера не являются способом обновления.

## 6. Проверка и восстановление

### Критерии готовности

Должны выполняться:

```bash
docker ps --filter name=infra-runtime
curl -fsS http://127.0.0.1:3000/api/ping
test -s /mnt/persistent-state/semaphore/semaphore.sqlite
test -s /run/infra-manager/secrets/semaphore-server.env
test -s /run/infra-manager/secrets/initial-admin-password
test -s /run/infra-manager/secrets/semaphore-api-token
# итоговая операторская проверка выполняется на PVE:
infra-manager status
```

Дополнительно через API нужно подтвердить:

- ровно один проект с ожидаемым именем;
- ровно один репозиторий;
- ожидаемые группы переменных;
- целевой набор заданий;
- наличие задания синхронизации OTP;
- `max_parallel_tasks=1`.

### Восстановление

Обычный сбой работающего 910 исправляется с PVE:

```bash
infra-manager repair
```

Если 910 потерян или обычного исправления недостаточно:

```bash
infra-manager recover
```

Обе команды сохраняют прежний `state/semaphore`; SQLite и `SEMAPHORE_ACCESS_KEY_ENCRYPTION` должны восстанавливаться согласованно. После завершения единая операторская команда сама выполняет итоговый status.

### Типичные ошибки

Если порт 3000 не отвечает:

```bash
docker logs --tail 200 infra-runtime
test -s /run/infra-manager/secrets/semaphore-server.env
```

Если синхронизация не работает:

```bash
test -s /run/infra-manager/secrets/pve-api.env
test -s /run/infra-manager/secrets/github_proxmox_repo_ed25519
test -s /run/infra-manager/secrets/initial-admin-password
```

Если появились дубликаты объектов, автоматизация должна остановиться до ручного устранения неоднозначности.

## 7. Источники и связанные документы

Машинные источники целевого состояния:

- [`../provision.yaml`](../provision.yaml);
- [`../rootfs/opt/infra-manager/compose/docker-compose.yml`](../rootfs/opt/infra-manager/compose/docker-compose.yml);
- [`../rootfs/opt/infra-manager/compose/runtime/Dockerfile`](../rootfs/opt/infra-manager/compose/runtime/Dockerfile);
- Ansible-роль `infra_manager`;
- `scripts/infra-manager/infra_manager/semaphore.py`;
- `scripts/infra-manager/jobs/`.

Связанные документы:

- [`infra-runtime.md`](infra-runtime.md) — контейнер и безопасное самообновление;
- [`openbao.md`](openbao.md) — secrets, SSH CA и OTP;
- [`data-and-access.md`](data-and-access.md) — постоянная база и временные secrets;
- [`system-services.md`](system-services.md) — системный запуск;
- [`../../../../docs/700-security/730-git-access.md`](../../../../docs/700-security/730-git-access.md) — Git-контракт;
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — текущие переходные расхождения.
