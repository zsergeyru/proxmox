# Рабочая среда infra-runtime

**Тип:** локальная спецификация рабочей среды
**Статус:** целевое состояние
**Назначение:** описать, каким должен быть контейнер `infra-runtime` на 910, как его собрать, запустить, безопасно обновить и восстановить.

`infra-runtime` — основной рабочий контейнер 910. В нём работают Semaphore и инструменты управления инфраструктурой.

## 1. Состав и файлы

### Состав контейнера

Образ строится на основе:

```text
semaphoreui/semaphore:v2.18.30
```

и дополнительно содержит:

- Ansible из базового образа Semaphore;
- OpenTofu `1.12.6`;
- Packer `1.15.4`;
- Python 3;
- `proxmoxer`, `requests`, `PyYAML`, `jsonschema`;
- Git;
- OpenSSH client;
- `curl`, `openssl`, `unzip`, `bash` и служебные пакеты.

Точные версии задаются в [`../provision.yaml`](../provision.yaml).

OpenBao в `infra-runtime` не находится — он работает отдельным контейнером.

### Исходные файлы

Все файлы образа находятся в:

```text
infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/
```

Основные файлы:

```text
docker-compose.yml
runtime/Dockerfile
runtime/requirements.txt
runtime/ssh_config
openbao/openbao.hcl
```

Ansible копирует этот каталог в 910:

```text
/opt/infra-manager/compose/
```

Это делает задача `repository.yml` роли `infra_manager`.

## 2. Сборка образа

### Как собирается образ

**Базовый образ**

`Dockerfile` получает версию Semaphore через build argument:

```text
SEMAPHORE_VERSION
```

Сборка временно переключается на `root`, устанавливает пакеты и инструменты, а в конце возвращается к:

```text
USER 1001:0
```

**OpenTofu и Packer**

OpenTofu и Packer скачиваются во время сборки.

Для каждого архива выполняются:

1. определение архитектуры `x86_64 → amd64` или `aarch64 → arm64`;
2. загрузка архива;
3. загрузка официального файла SHA256;
4. проверка контрольной суммы;
5. установка двоичного файла в `/usr/local/bin`;
6. удаление временных файлов.

Сборка завершается ошибкой, если архитектура не поддерживается или SHA256 не совпадает.

**Python**

`runtime/requirements.txt` устанавливает:

```text
proxmoxer>=2.2.0,<3
requests>=2.32,<3
PyYAML==6.0.2
jsonschema==4.25.1
```

**SSH**

В образ копируется:

```text
/etc/ssh/ssh_config.d/99-infra-manager.conf
```

Он задаёт:

```text
StrictHostKeyChecking yes
UserKnownHostsFile /var/lib/semaphore/known_hosts
HashKnownHosts yes
```

Штатный файл Semaphore `semaphore.conf` удаляется, чтобы он не ослабил эту настройку.

### Файл версий

Перед сборкой Ansible создаёт:

```text
/opt/infra-manager/compose/.versions.env
```

Содержимое формируется из `provision.yaml`:

```text
SEMAPHORE_VERSION=<version>
RUNTIME_VERSION=<version>
OPENTOFU_VERSION=<version>
PACKER_VERSION=<version>
OPENBAO_VERSION=<version>
```

Этот файл воспроизводим и не является постоянным состоянием.

Проверить его:

```bash
cat /opt/infra-manager/compose/.versions.env
```

### Подключения контейнера

`infra-runtime` использует `network_mode: host`.

Подключения:

| Путь на 910 | Путь в контейнере | Режим |
|---|---|---|
| `/mnt/persistent-state/semaphore` | `/var/lib/semaphore` | RW |
| `/etc/infra-manager/ca` | `/etc/infra-manager/ca` | RO |
| `/mnt/pve-access/pve-host` | `/mnt/pve-access/pve-host` | RO |
| `/mnt/persistent-state/opentofu` | `/var/lib/infra-manager/opentofu` | RW |
| `/mnt/persistent-state/locks` | `/var/lib/infra-manager/locks` | RW |
| `/var/lib/infra-manager/bootstrap-repo` | `/var/lib/infra-manager/bootstrap-repo` | RO |
| `/run/infra-manager/secrets` | `/run/infra-manager/secrets` | RO |

Контейнер может менять свои рабочие данные Semaphore и OpenTofu, а также общий каталог координационных lock-файлов. `/var/lib/infra-manager/locks` указывает на тот же `/mnt/persistent-state/locks`, который использует операторский процесс на 910, поэтому PVE CLI и Semaphore видят одни и те же блокировки. Постоянная Git-копия подключена в контейнер только для чтения. Перед прямым `deploy` оператор автоматически обновляет её под исключительной блокировкой и создаёт временный каталог `/tmp/infra-manager-direct-*` внутри `infra-runtime`; `sync/repair/test` создают такой же снимок текущей копии под разделяемой блокировкой. После создания снимка блокировка проекта освобождается, а `guest-operation.py` выполняется уже из снимка. Поэтому самообновление управляющего гостя может менять постоянную Git-копию, не меняя файлы уже запущенного процесса. PVE-доступ, центр SSH-доверия и материализованные секреты контейнер получает только для чтения.

## 3. Запуск и проверка

### Подготовка 910

До сборки `infra-runtime` должны существовать:

```text
/opt/infra-manager/compose/
/mnt/persistent-state/semaphore/
/mnt/persistent-state/opentofu/
/mnt/persistent-state/locks/
/var/lib/infra-manager/bootstrap-repo/
/etc/infra-manager/ca/
/mnt/pve-access/pve-host/
/run/infra-manager/secrets/
```

Штатно каталоги и подключения создаёт Ansible.

Проверить внешние подключения:

```bash
mountpoint /mnt/persistent-state
mountpoint /mnt/pve-access
```

Проверить исходники:

```bash
test -f /opt/infra-manager/compose/docker-compose.yml
test -f /opt/infra-manager/compose/runtime/Dockerfile
test -f /opt/infra-manager/compose/.versions.env
```

### Команда сборки

Штатная команда из `runtime.yml`:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  build --pull runtime
```

`--pull` заставляет проверить более новый базовый образ указанной версии.

После сборки ожидается локальный образ:

```text
infra-runtime:v1
```

Точный тег берётся из `provision.yaml`.

Проверить:

```bash
docker image inspect infra-runtime:v1 >/dev/null
```

### Обычный запуск

При обычной настройке 910 Ansible сначала запускает OpenBao, затем весь Compose:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d --remove-orphans
```

Контейнер имеет:

```text
restart: unless-stopped
```

Проверить:

```bash
docker ps --filter name=infra-runtime
docker logs --tail 100 infra-runtime
curl -fsS http://127.0.0.1:3000/api/ping
```

### Проверка контейнера

Те же проверки выполняет Ansible в `verify.yml`:

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime packer version
docker exec infra-runtime ansible --version
docker exec infra-runtime python3 -c 'import proxmoxer, yaml, jsonschema'
```

Проверить подключённые каталоги:

```bash
docker exec infra-runtime test -d /var/lib/semaphore
docker exec infra-runtime test -d /var/lib/infra-manager/opentofu
docker exec infra-runtime test -w /var/lib/infra-manager/locks
docker exec infra-runtime test -r /var/lib/infra-manager/bootstrap-repo/scripts/infra-manager/jobs/guest-operation.py
docker exec infra-runtime test -d /run/infra-manager/secrets
```

Штатная операторская проверка выполняется с PVE:

```bash
infra-manager status
```

Внутренний `infra-manager-status --full` остаётся технической проверкой автоматики.

## 4. Самообновление 910

### Почему самообновление отличается

Единый `Deploy Guest` с выбранным `910 infra-manager` выполняется **внутри того же `infra-runtime`**, который это задание может изменить.

Если Ansible сразу выполнит обычный `docker compose up`, текущий контейнер может быть уничтожен до завершения собственного задания.

Поэтому при `infra_self_update=true` задача `runtime.yml`:

- собирает новый образ;
- запускает/обновляет OpenBao;
- **не запускает новый `infra-runtime` сразу**.

После завершения настройки playbook создаёт внешнюю временную systemd-службу:

```text
infra-manager-runtime-activate-<epoch>
```

через:

```text
systemd-run
```

Она запускает:

```text
/usr/local/sbin/infra-manager-activate-runtime
```

и передаёт:

```text
INFRA_PROJECT_BRANCH
INFRA_PVE_NODE
```

### Признак отложенной активации

Во время самообновления используется один физический файл, видимый под разными путями:

```text
на 910:          /mnt/persistent-state/semaphore/.infra-manager-runtime-activation-pending
в infra-runtime: /var/lib/semaphore/.infra-manager-runtime-activation-pending
```

В файл записывается пространство PID и PID процесса, который запустил самообновление:

```text
host:<PID>
runtime:<PID>
```

Штатные гостевые операции как с PVE, так и из Semaphore выполняются внутри `infra-runtime` и используют `runtime:<PID>`. Формат `host:<PID>` поддерживается для служебного host-side исполнения общего механизма, а для безопасного первого перехода активация также принимает старый числовой формат как `runtime:<PID>`.

Новые инфраструктурные операции проверяют этот признак и не должны начинать изменение инфраструктуры во время замены среды.

### Как работает активация

`infra-manager-activate-runtime` пишет журнал:

```text
/var/log/infra-manager/runtime-activation.log
```

Порядок:

1. читает пространство PID и PID из файла-признака;
2. проверяет формат `host:<PID>`, `runtime:<PID>` или совместимый старый числовой формат;
3. для `host` ждёт процесс через `kill -0` на 910, а для `runtime` — через `docker exec ... kill -0` в старом `infra-runtime`;
4. максимальное ожидание — 15 минут;
5. выполняет `docker compose up -d --remove-orphans` уже с новым образом;
6. выполняет `infra-manager-openbao-startup-unseal`;
7. до 120 секунд ждёт `http://127.0.0.1:3000/api/ping`;
8. выполняет `python3 -m infra_manager semaphore-project`;
9. выполняет `infra-manager-status --full --quiet`;
10. удаляет файл-признак при завершении, включая ошибку.

Если `INFRA_PVE_NODE` не задан, PID некорректен, задание не завершилось или Semaphore не поднялся, активация завершается ошибкой.

### Наблюдение за активацией

На 910:

```bash
tail -f /var/log/infra-manager/runtime-activation.log
```

Проверить временную systemd-службу:

```bash
systemctl list-units 'infra-manager-runtime-activate-*'
```

Проверить признак:

```bash
ls -l /mnt/persistent-state/semaphore/.infra-manager-runtime-activation-pending
```

После успешной активации признак должен исчезнуть.

## 5. Эксплуатация и обновление

### Ручная остановка и запуск

Остановить `infra-runtime`:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  stop runtime
```

Запустить:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d runtime
```

После ручного запуска проверить:

```bash
curl -fsS http://127.0.0.1:3000/api/ping
infra-manager-status --full
```

Если одновременно менялись OpenBao или рабочие секреты, безопаснее запускать весь Compose через штатный механизм активации.

### Обновление состава образа

Изменения делаются только через Git.

Типичные источники:

- версия Semaphore — `provision.yaml`;
- версия OpenTofu — `provision.yaml`;
- версия Packer — `provision.yaml`;
- Python-библиотеки — `rootfs/.../runtime/requirements.txt` и согласованное описание в `provision.yaml`;
- системные пакеты — `rootfs/.../runtime/Dockerfile` и `provision.yaml`;
- SSH-настройка — `rootfs/.../runtime/ssh_config`.

После изменения выполняется `Deploy Guest 910`.

Не следует устанавливать пакеты вручную в уже работающий контейнер: после следующей сборки такие изменения исчезнут.

## 6. Восстановление и неисправности

### Восстановление infra-runtime

`infra-runtime` сам по себе не содержит уникального постоянного состояния.

Если контейнер или Docker-образ потерян:

1. восстановить подключения `state` и `access`;
2. установить Compose-файлы из `rootfs`;
3. создать `.versions.env` из `provision.yaml`;
4. убедиться, что OpenBao можно запустить и разблокировать;
5. убедиться, что рабочие секреты материализованы;
6. выполнить `docker compose ... build --pull runtime`;
7. выполнить `docker compose ... up -d --remove-orphans`;
8. проверить `/api/ping` Semaphore;
9. синхронизировать проект Semaphore;
10. выполнить `infra-manager-status --full`.

Нельзя считать восстановлением создание новых пустых каталогов Semaphore/OpenTofu вместо утраченного постоянного состояния.

### Типичные неисправности

**Сборка образа не проходит**

Проверить:

```bash
cat /opt/infra-manager/compose/.versions.env
docker compose --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml build runtime
```

Если ошибка связана с SHA256 OpenTofu или Packer, не обходить проверку контрольной суммы.

**Контейнер постоянно перезапускается**

Проверить:

```bash
docker ps -a --filter name=infra-runtime
docker logs --tail 200 infra-runtime
test -s /run/infra-manager/secrets/semaphore-server.env
```

Также проверить владельца `/mnt/persistent-state/semaphore`.

**Самообновление зависло**

Проверить:

```bash
cat /var/lib/semaphore/.infra-manager-runtime-activation-pending
tail -n 200 /var/log/infra-manager/runtime-activation.log
systemctl list-units 'infra-manager-runtime-activate-*'
```

Не удалять старый контейнер вручную, пока не понятно, завершилось ли выполняющееся внутри него задание.

## 7. Источники и связанные документы

- [`../provision.yaml`](../provision.yaml) — версии, образ и подключения.
- [`../rootfs/opt/infra-manager/compose/docker-compose.yml`](../rootfs/opt/infra-manager/compose/docker-compose.yml) — Compose.
- [`../rootfs/opt/infra-manager/compose/runtime/Dockerfile`](../rootfs/opt/infra-manager/compose/runtime/Dockerfile) — сборка образа.
- [`../rootfs/opt/infra-manager/compose/runtime/requirements.txt`](../rootfs/opt/infra-manager/compose/runtime/requirements.txt) — Python-библиотеки.
- [`../rootfs/opt/infra-manager/compose/runtime/ssh_config`](../rootfs/opt/infra-manager/compose/runtime/ssh_config) — SSH-настройка.
- [`semaphore.md`](semaphore.md) — Semaphore внутри контейнера.
- [`openbao.md`](openbao.md) — отдельный контейнер OpenBao.
- [`data-and-access.md`](data-and-access.md) — подключаемые данные.
- [`system-services.md`](system-services.md) — внешняя активация и systemd.
- [`../../../../automation/ansible/roles/infra_manager/tasks/runtime.yml`](../../../../automation/ansible/roles/infra_manager/tasks/runtime.yml) — фактическая сборка и запуск.
- [`../../../../automation/ansible/playbooks/configure-guest.yml`](../../../../automation/ansible/playbooks/configure-guest.yml) — назначение отложенной активации.
- [`../../../../scripts/infra-manager/commands/activate-runtime.sh`](../../../../scripts/infra-manager/commands/activate-runtime.sh) — фактическая логика активации.