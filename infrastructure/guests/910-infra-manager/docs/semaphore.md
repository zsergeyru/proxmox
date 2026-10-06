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
Views:         Guests / Infrastructure / Security
Templates:     целевой набор инфраструктурных заданий
```

Semaphore сам создаёт встроенную группу `All` с позицией `0`. Управляемые проектом группы должны идти после неё: `Guests=1`, `Infrastructure=2`, `Security=3`.

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

Повторная синхронизация должна сохранять выбранное допустимое значение.

Оператор меняет его штатным заданием `Set Log Level` в группе `Infrastructure`. В форме запуска отображается список:

| Название | Сохраняемое значение |
|---|---|
| `Обычный` | `normal` |
| `Подробный` | `verbose` |
| `Тихий` | `quiet` |

Выбранное значение записывается в существующую Variable Group `Infra Manager` через API Semaphore и применяется к следующим заданиям. Задание не изменяет Git, OpenBao, PVE или другие секреты.

### Группы шаблонов

В разделе Task Templates должны существовать три управляемые группы Semaphore Views:

| View | Позиция | Содержимое |
|---|---:|---|
| `Guests` | 1 | `Deploy Guest`, `Status Guest`, `Repair Guest`, `Test Guest`, `Sync Guest` |
| `Infrastructure` | 2 | `OpenTofu Plan`, `Build Template 9000`, `Set Log Level` |
| `Security` | 3 | `Sync SSH Access` |

Views являются частью декларативной настройки проекта. `semaphore-project` должен создавать отсутствующие Views, исправлять их позиции и записывать соответствующий `view_id` в каждый управляемый шаблон. Ручные дополнительные Views не удаляются.

### Целевые задания

Semaphore должен содержать как минимум:

| Задание | Назначение |
|---|---|
| `OpenTofu Plan` | показать изменения инфраструктуры |
| `Build Template 9000` | собрать базовый шаблон Debian |
| `Set Log Level` | выбрать постоянный режим вывода для следующих заданий |
| `Deploy Guest` | полностью привести выбранный гость к `guest.yaml + provision.yaml` |
| `Status Guest` | проверить состояние выбранного гостя без изменений |
| `Repair Guest` | выполнить только безопасные повторяемые исправления |
| `Test Guest` | выполнить расширенную проверку работающего гостя |
| `Sync Guest` | синхронизировать изменяемые данные и настройки без полного Deploy |
| `Sync SSH Access` | синхронизировать AppRole, policy и OTP-роли OpenBao из `access.yaml` |

Пять гостевых заданий используют одну обязательную survey-переменную `GUEST_VMID` типа `enum`. Список вариантов строится из машинного каталога проекта. `Deploy`, `Status`, `Repair` и `Test` доступны гостям с `guest.yaml + provision.yaml`; `Sync` показывается только для ролей, у которых реализован безопасный обработчик синхронизации.

Произвольный VMID через свободные аргументы Semaphore не разрешается. Все пять шаблонов вызывают общий Python-диспетчер `scripts/infra-manager/jobs/guest-operation.py`, а различие операций задаётся фиксированным аргументом самого шаблона.

Первичная команда `scripts/infra-manager/jobs/initialize-openbao.py` сохраняется как внутренний механизм bootstrap/recovery и не должна отображаться как обычный шаблон Semaphore. Оператор использует только PVE-команды `infra-manager repair` и `infra-manager recover`.

Задание `Sync SSH Access` должно выполнять только идемпотентную синхронизацию OpenBao, описанную в [`openbao.md`](openbao.md). Оно не развёртывает гостей и не выполняет сквозные SSH-тесты. Настройка гостя относится к `Deploy Guest`, а приёмочная проверка выполняется отдельным сценарием `scripts/acceptance/verify-ssh-access.py`.

## 5. Жизненный цикл и самообновление

### Sync Guest

`Sync Guest` не является сокращённым `Deploy Guest`. Он не должен менять ресурсы VM/LXC, устанавливать системные пакеты или пересобирать `infra-runtime`.

Semaphore перед каждым заданием получает рабочую копию выбранной Git-ветки. Поэтому `Sync Guest` использует уже свежую версию проекта текущего задания и применяет только разрешённую для роли лёгкую синхронизацию.

Для роли `infra-manager` текущая синхронизация обновляет через API Semaphore:

- Git-репозиторий существующего проекта;
- группы переменных;
- Views и набор шаблонов;
- списки гостей в формах запуска.

`Sync Guest` выполняется от пользователя `1001` внутри `infra-runtime` и использует уже существующий рабочий API token Semaphore. Этот путь не должен перевыпускать token, записывать секреты в `/run` или обновлять значение token в OpenBao.

Он также не должен перезаписывать существующий SSH key `GitHub project read-only`: Semaphore v2.18.30 при обновлении используемого репозиторием ключа очищает его рабочий cache, включая checkout текущего задания. `Sync Guest` только проверяет существующие Git key/repository и при необходимости обновляет безопасные настройки, не разрушающие текущую рабочую копию.

Если API token, Git key или Git repository отсутствуют либо не соответствуют ожидаемому состоянию, синхронизация останавливается и требует `Repair Guest` для `infra-manager`.

Пересборка или перезапуск 910 для такой синхронизации не требуется. Для других ролей обработчики `Sync` добавляются отдельно; пока обработчика нет, такой гость не должен появляться в списке `Sync Guest`.

### Обычная синхронизация

После настройки 910 необходимо:

1. дождаться Semaphore;
2. проверить инструменты `infra-runtime`;
3. выполнить `semaphore-project`;
4. проверить целевой набор объектов;
5. выполнить `infra-manager-status --full --quiet`.

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
- Views `Guests`, `Infrastructure`, `Security` и их порядок;
- правильную принадлежность каждого управляемого шаблона к View;
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
