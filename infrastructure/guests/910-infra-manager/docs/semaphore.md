# Semaphore на 910 infra-manager

**Назначение:** практическое руководство по созданию, настройке, проверке, обновлению и восстановлению Semaphore на 910.

Общие документы проекта объясняют, зачем Semaphore используется как исполнитель инфраструктурных заданий. Этот файл описывает его конкретную реализацию внутри 910.

## 1. Где работает Semaphore

Semaphore не имеет отдельного Docker-контейнера. Он является базой контейнера:

```text
infra-runtime
```

который собирается из:

```text
rootfs/opt/infra-manager/compose/runtime/Dockerfile
```

Базовый образ:

```text
semaphoreui/semaphore:v2.18.30
```

Точная версия находится в [`../provision.yaml`](../provision.yaml).

Контейнер использует:

```text
network_mode: host
```

поэтому Semaphore слушает порт `3000` самого 910.

Рабочий адрес:

```text
http://<IP-910>:3000
```

Локальная API-точка внутри 910:

```text
http://127.0.0.1:3000
```

## 2. Постоянные данные

Используется SQLite.

Путь внутри контейнера:

```text
/var/lib/semaphore/semaphore.sqlite
```

Compose подключает:

```text
/mnt/persistent-state/semaphore
        ↓
/var/lib/semaphore
```

На уровне 910 тот же постоянный каталог также доступен как:

```text
/var/lib/infra-manager/semaphore
    → /mnt/persistent-state/semaphore
```

В каталоге Semaphore сохраняются база, история заданий, настройки проекта и `known_hosts` Git.

Перед запуском Ansible приводит владельца и права к:

```text
владелец: 1001:0
каталоги: 0770
файлы:   0660
```

## 3. Что должно быть готово до запуска

Semaphore зависит от нескольких частей 910:

1. Docker установлен и запущен.
2. `/mnt/persistent-state/semaphore` подключён с PVE.
3. `/run/infra-manager/secrets/` создан.
4. существует `semaphore-server.env`.
5. существует `initial-admin-password`.
6. доступен рабочий Git credential.
7. после инициализации OpenBao рабочие PVE/Git/Semaphore значения материализованы из него.
8. контейнер `infra-runtime` собран.

Штатно это готовят задачи:

```text
automation/ansible/roles/infra_manager/tasks/persistence.yml
automation/ansible/roles/infra_manager/tasks/semaphore.yml
automation/ansible/roles/infra_manager/tasks/runtime.yml
```

## 4. Первый запуск Semaphore

До первой инициализации OpenBao Ansible может создать первоначальные данные Semaphore.

### 4.1. Первоначальный пароль

Если файл:

```text
/run/infra-manager/secrets/semaphore-server.env
```

ещё отсутствует, Ansible генерирует:

- пароль `admin` через `openssl rand -base64 24`;
- ключ `SEMAPHORE_ACCESS_KEY_ENCRYPTION` через `openssl rand -base64 32`.

Затем создаются два файла:

```text
/run/infra-manager/secrets/semaphore-server.env
/run/infra-manager/secrets/initial-admin-password
```

Оба принадлежат:

```text
1001:0
mode 0600
```

### 4.2. Содержимое semaphore-server.env

Текущий набор переменных:

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

Compose передаёт этот файл контейнеру через:

```text
env_file:
  /run/infra-manager/secrets/semaphore-server.env
```

### 4.3. После инициализации OpenBao

После готовности OpenBao постоянным источником пароля и ключа шифрования становится KV OpenBao.

Файлы под `/run/infra-manager/secrets/` остаются только временным рабочим представлением.

Если `semaphore-server.env` существует, но `initial-admin-password` восстановить не удалось, Ansible считает состояние некорректным и останавливается.

## 5. Запуск контейнера

Semaphore запускается вместе с `infra-runtime`.

Штатная команда:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d --remove-orphans
```

Проверить контейнер:

```bash
docker ps --filter name=infra-runtime
docker logs --tail 100 infra-runtime
```

Проверить сам Semaphore:

```bash
curl -fsS http://127.0.0.1:3000/api/ping
```

Ansible ждёт порт `3000` до 120 секунд.

## 6. Учётная запись администратора

Первоначальный пользователь:

```text
login: admin
```

Пароль хранится в рабочем файле:

```text
/run/infra-manager/secrets/initial-admin-password
```

Его можно увидеть через:

```bash
infra-manager-status --full
```

или от `root`:

```bash
cat /run/infra-manager/secrets/initial-admin-password
```

Пароль не должен попадать в обычный журнал.

## 7. Автоматическое создание проекта

После запуска Semaphore 910 выполняет:

```bash
PYTHONPATH=/usr/local/lib/infra-manager \
python3 -m infra_manager semaphore-project
```

Основная реализация:

```text
scripts/infra-manager/infra_manager/semaphore.py
```

Команда должна выполняться от `root` на 910.

### 7.1. Что синхронизируется

Команда создаёт или приводит к ожидаемому виду:

```text
Project:       Proxmox Infrastructure
SSH key:       GitHub project read-only
Repository:    proxmox
VariableGroup: OpenTofu PVE
VariableGroup: Infra Manager
Templates:     инфраструктурные задания
```

Проект создаётся с:

```text
max_parallel_tasks = 1
```

Это не даёт двум инфраструктурным заданиям одновременно менять один контур.

Числовой ID проекта сохраняется в:

```text
/var/lib/infra-manager/semaphore-project-id
```

Если Semaphore содержит несколько объектов с одинаковым ожидаемым именем, синхронизация завершается ошибкой вместо случайного выбора.

## 8. Git-репозиторий Semaphore

Репозиторий:

```text
git@github.com:zsergeyru/proxmox.git
```

Имя объекта Semaphore:

```text
proxmox
```

SSH-ключ:

```text
GitHub project read-only
```

Рабочая закрытая часть ключа:

```text
/run/infra-manager/secrets/github_proxmox_repo_ed25519
```

Внутри `infra-runtime` OpenSSH использует:

```text
StrictHostKeyChecking yes
UserKnownHostsFile /var/lib/semaphore/known_hosts
```

Файл `known_hosts` готовится Ansible в постоянном каталоге Semaphore.

Ветка синхронизируется с `INFRA_PROJECT_BRANCH`; если переменная не задана, используется `main`.

## 9. Группы переменных

### 9.1. OpenTofu PVE

Группа создаётся из рабочего файла:

```text
/run/infra-manager/secrets/pve-api.env
```

и открытого ключа Ansible.

В обычные переменные Semaphore записываются:

```text
TF_VAR_pve_endpoint
TF_VAR_ansible_ssh_public_key
```

Секретной переменной хранится:

```text
TF_VAR_pve_api_token
```

При повторной синхронизации существующий secret обновляется, а не добавляется вторым.

### 9.2. Infra Manager

Группа содержит общие настройки выполнения заданий.

Сейчас главный параметр:

```text
INFRA_LOG_LEVEL
```

Допустимые значения:

| Значение | Когда использовать |
|---|---|
| `normal` | обычная работа |
| `verbose` | разработка и диагностика |
| `quiet` | минимальный вывод |

При первом создании записывается `normal`.

Если оператор вручную выбрал допустимое значение в интерфейсе Semaphore, синхронизация его сохраняет.

Некорректное значение блокирует синхронизацию.

## 10. Задания Semaphore

Точный набор задаётся `SEMAPHORE_TEMPLATES` в `scripts/infra-manager/infra_manager/semaphore.py`.

Текущий состав:

| Задание | Точка входа | Аргументы |
|---|---|---|
| `OpenTofu Plan` | `scripts/infra-manager/jobs/opentofu-plan.py` | нет |
| `Build Template 9000` | `scripts/infra-manager/jobs/build-template.py` | `9000` |
| `Deploy Guest 410` | `scripts/infra-manager/jobs/deploy-guest.py` | `410` |
| `Deploy Guest 910` | `scripts/infra-manager/jobs/deploy-guest.py` | `910` |
| `Sync Machine SSH` | `scripts/infra-manager/jobs/sync-machine-ssh.py` | нет |
| `Initialize OpenBao 910` | `scripts/infra-manager/jobs/initialize-openbao.py` | нет |

Каждое задание привязано к репозиторию проекта и двум группам переменных.

`Sync Machine SSH` относится к переходной реализации и будет изменён вместе с переходом на OpenBao SSH OTP.

## 11. API-токен Semaphore

Для синхронизации используется API Semaphore.

Рабочий файл:

```text
/run/infra-manager/secrets/semaphore-api-token
```

Алгоритм:

1. если файл существует и `/user/` принимает токен — он переиспользуется;
2. если токен отсутствует или не работает — код входит как `admin`;
3. создаёт токен с именем `infra-manager setup`;
4. записывает его с режимом `0600`;
5. проверяет новый токен;
6. если OpenBao уже материализован — сохраняет новое значение обратно в OpenBao.

Таким образом, перевыпуск токена не должен приводить к расхождению между Semaphore и OpenBao.

## 12. Когда выполняется синхронизация

Синхронизация проекта запускается в двух основных случаях.

### 12.1. Обычная настройка 910

В конце роли `infra_manager` задача `verify.yml`:

1. ждёт Semaphore;
2. проверяет OpenTofu, Packer, Ansible и Python-библиотеки;
3. выполняет `python3 -m infra_manager semaphore-project`;
4. выполняет `infra-manager-status --full --quiet`.

### 12.2. Самообновление 910

Когда `Deploy Guest 910` меняет сам `infra-runtime`, старый контейнер нельзя уничтожать до завершения задания.

После его завершения внешняя команда `infra-manager-activate-runtime`:

1. запускает новый Compose;
2. разблокирует OpenBao;
3. ждёт `/api/ping` Semaphore;
4. синхронизирует проект;
5. выполняет полную тихую проверку.

Подробности находятся в [`infra-runtime.md`](infra-runtime.md).

## 13. Ручная проверка

Проверить порт:

```bash
curl -fsS http://127.0.0.1:3000/api/ping
```

Проверить постоянную базу:

```bash
test -s /mnt/persistent-state/semaphore/semaphore.sqlite
ls -lh /mnt/persistent-state/semaphore/semaphore.sqlite
```

Проверить рабочие файлы:

```bash
ls -l /run/infra-manager/secrets/semaphore-server.env
ls -l /run/infra-manager/secrets/initial-admin-password
ls -l /run/infra-manager/secrets/semaphore-api-token
```

Повторно синхронизировать проект:

```bash
PYTHONPATH=/usr/local/lib/infra-manager \
INFRA_PROJECT_BRANCH=main \
python3 -m infra_manager semaphore-project
```

Полная проверка:

```bash
infra-manager-status --full
```

## 14. Обновление Semaphore

Версия Semaphore является версией базового образа `infra-runtime`.

Для штатного обновления:

1. изменить `docker.services.runtime.base_image` в `provision.yaml`;
2. выполнить `Deploy Guest 910`;
3. Ansible перепишет `.versions.env`;
4. пересоберёт `infra-runtime` с `--pull`;
5. отложенная активация дождётся окончания текущего задания;
6. новый контейнер подключит прежний `/mnt/persistent-state/semaphore`;
7. проект Semaphore будет повторно синхронизирован;
8. `infra-manager-status --full` подтвердит результат.

Ручное изменение файлов внутри контейнера не является штатным обновлением.

Перед сменой версии нужно учитывать совместимость SQLite и `SEMAPHORE_ACCESS_KEY_ENCRYPTION`: постоянную базу и её ключ защиты следует рассматривать как связанные данные.

## 15. Восстановление Semaphore

Если `infra-runtime` потерян, но постоянные данные сохранились:

1. подключить прежний `/mnt/persistent-state/semaphore`;
2. восстановить или разблокировать OpenBao;
3. убедиться, что `semaphore-server.env` и `initial-admin-password` материализованы;
4. восстановить Git credential и PVE API рабочие файлы;
5. собрать `infra-runtime` из `rootfs/opt/infra-manager/compose/runtime/`;
6. запустить Compose;
7. дождаться `http://127.0.0.1:3000/api/ping`;
8. выполнить `python3 -m infra_manager semaphore-project`;
9. проверить задания, репозиторий и группы переменных;
10. выполнить `infra-manager-status --full`.

Если сохранена только пустая новая SQLite, прежняя история Semaphore не восстановлена.

Если база сохранена, но потерян соответствующий `SEMAPHORE_ACCESS_KEY_ENCRYPTION`, зашифрованные значения Semaphore могут оказаться непригодны. Поэтому база и секреты OpenBao должны восстанавливаться согласованно.

## 16. Типичные неисправности

### 16.1. Порт 3000 не отвечает

Проверить:

```bash
docker ps --filter name=infra-runtime
docker logs --tail 200 infra-runtime
test -s /run/infra-manager/secrets/semaphore-server.env
```

### 16.2. Semaphore запускается, но синхронизация не работает

Проверить:

```bash
test -s /run/infra-manager/secrets/pve-api.env
test -s /run/infra-manager/secrets/github_proxmox_repo_ed25519
test -s /run/infra-manager/secrets/initial-admin-password
```

Затем выполнить синхронизацию вручную в `verbose`-режиме через группу `Infra Manager` или с соответствующей переменной окружения задания.

### 16.3. Появились дубликаты объектов

Код специально останавливается при нескольких проектах, ключах, репозиториях или группах с одним ожидаемым именем.

Такие дубликаты нужно сначала разобрать вручную; синхронизация не должна угадывать, какой объект считать правильным.

### 16.4. API-токен перестал работать

Повторная синхронизация должна войти через `admin`, создать новый `infra-manager setup`, проверить его и записать новое значение в OpenBao.

## 17. Связанные документы

- [`../provision.yaml`](../provision.yaml) — версия Semaphore, пути и перечень объектов.
- [`../status.yaml`](../status.yaml) — итоговые проверки и вывод пароля.
- [`../rootfs/opt/infra-manager/compose/docker-compose.yml`](../rootfs/opt/infra-manager/compose/docker-compose.yml) — запуск `infra-runtime`.
- [`../rootfs/opt/infra-manager/compose/runtime/Dockerfile`](../rootfs/opt/infra-manager/compose/runtime/Dockerfile) — образ с Semaphore.
- [`infra-runtime.md`](infra-runtime.md) — сборка и безопасная активация контейнера.
- [`openbao.md`](openbao.md) — источник постоянных рабочих секретов.
- [`data-and-access.md`](data-and-access.md) — постоянная база и временные секреты.
- [`../../../../automation/ansible/roles/infra_manager/tasks/semaphore.yml`](../../../../automation/ansible/roles/infra_manager/tasks/semaphore.yml) — первый запуск и рабочие файлы.
- [`../../../../scripts/infra-manager/infra_manager/semaphore.py`](../../../../scripts/infra-manager/infra_manager/semaphore.py) — синхронизация проекта через API.
- [`../../../../docs/700-security/730-git-access.md`](../../../../docs/700-security/730-git-access.md) — роль Git-доступа в проекте.
- [`../../../../docs/700-security/740-openbao.md`](../../../../docs/700-security/740-openbao.md) — роль OpenBao и секретов.
- [`../../../../docs/800-operations/830-recovery.md`](../../../../docs/800-operations/830-recovery.md) — общий аварийный процесс.