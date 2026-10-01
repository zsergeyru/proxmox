# OpenBao на 910 infra-manager

**Назначение:** практическое руководство по развёртыванию и сопровождению OpenBao внутри 910.

Общий документ проекта [`740-openbao.md`](../../../../docs/700-security/740-openbao.md) объясняет, зачем проекту нужны KV, AppRole, SSH CA и SSH OTP. Этот файл показывает, **как OpenBao реально создаётся и работает на 910**.

## 1. Что разворачивается на 910

OpenBao работает отдельным Docker-контейнером:

```text
container: openbao
image:     ghcr.io/openbao/openbao:2.7.0
network:   host
restart:   unless-stopped
```

Точная версия берётся из [`../provision.yaml`](../provision.yaml).

Контейнер описан в:

```text
rootfs/opt/infra-manager/compose/docker-compose.yml
```

Конфигурация OpenBao:

```text
rootfs/opt/infra-manager/compose/openbao/openbao.hcl
```

При настройке 910 Ansible копирует каталог `rootfs/opt/infra-manager/compose/` в:

```text
/opt/infra-manager/compose/
```

Поэтому рабочая конфигурация внутри 910 находится в:

```text
/opt/infra-manager/compose/docker-compose.yml
/opt/infra-manager/compose/openbao/openbao.hcl
```

## 2. Сеть и конфигурация

OpenBao использует сеть самого 910.

Текущий listener:

```text
API:     127.0.0.1:8200
cluster: 127.0.0.1:8201
TLS:     выключен только для локального loopback
UI:      выключен
```

Эти значения находятся в `rootfs/opt/infra-manager/compose/openbao/openbao.hcl`.

Внутри контейнера задаётся:

```text
BAO_ADDR=http://127.0.0.1:8200
BAO_LOG_LEVEL=info
```

С текущей конфигурацией API OpenBao не слушает внешний адрес 910. Поэтому обычная проверка выполняется локально из 910.

## 3. Постоянное состояние

OpenBao использует Raft.

Путь внутри контейнера:

```text
/openbao/file/raft
```

Compose подключает к `/openbao/file` постоянный каталог 910:

```text
/mnt/persistent-state/openbao
```

Физически он приходит с PVE из области:

```text
/mnt/bindmounts/infra-manager/state/openbao
```

На уровне 910 дополнительно существует привычная ссылка:

```text
/var/lib/persistent/openbao
    → /mnt/persistent-state/openbao
```

Идентификатор Raft-узла:

```text
infra-manager-910
```

Именно этот каталог содержит состояние, которое нельзя заменить новым пустым OpenBao при восстановлении.

## 4. Что должно существовать до запуска

Перед развёртыванием OpenBao должны быть готовы:

1. Docker на 910.
2. Подключение постоянного состояния `/mnt/persistent-state`.
3. Подключение доступа `/mnt/pve-access` только для чтения.
4. Каталог `/mnt/persistent-state/openbao`.
5. Рабочая копия проекта `/var/lib/infra-manager/bootstrap-repo`.
6. Compose-файлы в `/opt/infra-manager/compose`.
7. SSH-доступ 910 → PVE root через `/mnt/pve-access/pve-host/`.

Эти предварительные условия готовят задачи роли:

```text
automation/ansible/roles/infra_manager/tasks/persistence.yml
automation/ansible/roles/infra_manager/tasks/pve_access.yml
automation/ansible/roles/infra_manager/tasks/repository.yml
```

Если подключения `access` или `state` отсутствуют, настройка 910 должна завершиться ошибкой до создания локальных замен.

## 5. Установка файлов и запуск контейнера

Ansible выполняет следующие действия.

### 5.1. Устанавливает Compose-файлы

Исходник:

```text
infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/
```

Рабочее место:

```text
/opt/infra-manager/compose/
```

### 5.2. Создаёт файл версий

Ansible записывает:

```text
/opt/infra-manager/compose/.versions.env
```

В нём находятся:

```text
SEMAPHORE_VERSION
RUNTIME_VERSION
OPENTOFU_VERSION
PACKER_VERSION
OPENBAO_VERSION
```

Файл воспроизводится из `provision.yaml` и отдельно резервировать его не требуется.

### 5.3. Запускает OpenBao

Штатная команда, которую выполняет роль:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d openbao
```

После этого Ansible ждёт появления `127.0.0.1:8200` до 120 секунд.

Проверить контейнер вручную можно так:

```bash
docker ps --filter name=openbao
curl -fsS http://127.0.0.1:8200/v1/sys/seal-status | jq
```

## 6. Первичная инициализация

Контейнер OpenBao специально **не инициализируется автоматически** при обычном запуске.

В `provision.yaml` это зафиксировано как:

```text
auto_initialize: false
```

Первичная инициализация выполняется отдельным заданием Semaphore:

```text
Initialize OpenBao 910
```

Точка входа задания:

```text
scripts/infra-manager/jobs/initialize-openbao.py
```

Основная логика:

```text
scripts/infra-manager/infra_manager/openbao.py
scripts/infra-manager/host/openbao-unseal.py
```

### 6.1. Что делает задание

В текущей реализации порядок такой:

1. определяет PVE-узел из `TF_VAR_pve_endpoint`;
2. устанавливает или обновляет на PVE служебные сценарии OpenBao;
3. подготавливает аварийный Git-контур;
4. проверяет, был ли OpenBao уже инициализирован;
5. если OpenBao пустой — выполняет `/v1/sys/init` со схемой `1 ключ / порог 1`;
6. сохраняет unseal key только в PVE-only области;
7. разблокирует OpenBao;
8. создаёт два SSH CA;
9. создаёт ограниченные служебные AppRole;
10. создаёт KV v2 и переносит туда рабочие секреты;
11. материализует временные рабочие файлы для 910;
12. публикует открытые ключи SSH CA;
13. создаёт и проверяет роли подписи SSH;
14. отзывает initial root token;
15. проверяет аварийный контур;
16. удаляет переходные постоянные файловые источники секретов.

### 6.2. Что появляется только на PVE

Канонический каталог:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao/
```

Текущие служебные файлы:

```text
unseal.key
ssh-access.json
kv-access.json
```

`unseal.key` нужен для разблокировки.

`ssh-access.json` и `kv-access.json` содержат ограниченные служебные данные доступа PVE к OpenBao.

Эти файлы **не подключаются внутрь 910 как постоянные файлы**.

### 6.3. Защита от опасной повторной инициализации

Код специально останавливается, если:

- OpenBao уже инициализирован, но PVE не имеет ожидаемого unseal key;
- OpenBao ещё пустой, но в PVE-only каталоге уже лежат старые `unseal.key`, `ssh-access.json` или `kv-access.json`;
- существующие SSH CA имеют неожиданное состояние;
- ограниченные AppRole не проходят проверку.

То есть повторный запуск задания должен продолжать существующий OpenBao, а не молча создавать новый центр доверия.

## 7. Какие функции OpenBao реально создаются

На 910 сейчас настраиваются следующие механизмы.

### 7.1. KV v2

Mount:

```text
infra-secrets/
```

В нём хранятся рабочие значения управляющего контура, в том числе:

```text
PVE API
GitHub read-only Deploy Key
Semaphore
```

Рабочие программы не читают Raft напрямую. Нужные значения материализуются во временные файлы.

### 7.2. SSH client CA

Mount:

```text
ssh-client-signer
```

Открытый ключ публикуется в:

```text
/etc/infra-manager/ca/ssh-client-ca.pub
```

Он используется для административных SSH-сертификатов.

### 7.3. SSH host CA

Mount:

```text
ssh-host-signer
```

Открытый ключ публикуется в:

```text
/etc/infra-manager/ca/ssh-host-ca.pub
```

Он используется для подписания реальных SSH host key управляемых Linux-гостей.

### 7.4. Служебные AppRole

Для операций 910/PVE создаётся отдельное пространство:

```text
auth/infra-manager
```

Среди используемых ролей есть отдельные роли для:

- настройки SSH CA;
- подписи SSH-ключей;
- чтения рабочих KV-секретов;
- обновления API-токена Semaphore.

Рабочие токены этих ролей создаются на время операции и не заменяют root token.

### 7.5. Переходный межмашинный SSH

Код пока содержит старые роли `machine-<VMID>` и механизм периодической подписи машинных SSH-ключей.

Он остаётся фактической переходной реализацией до завершения перехода на OpenBao SSH OTP. Текущее состояние описано в [`780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md).

## 8. Рабочие секреты внутри 910

После готовности OpenBao рабочие файлы создаются в:

```text
/run/infra-manager/secrets/
```

Текущий набор включает, в частности:

```text
pve-api.env
github_proxmox_repo_ed25519
semaphore-server.env
initial-admin-password
semaphore-api-token
```

Эта директория временная. После перезапуска 910 она должна быть восстановлена из OpenBao через PVE-only механизм.

До первой инициализации временные bootstrap-значения могут находиться в:

```text
/run/infra-manager/bootstrap-secrets/
```

После успешного переноса в OpenBao они больше не являются штатным источником.

## 9. Разблокировка после перезапуска

OpenBao после запуска контейнера может быть запечатан.

На 910 устанавливается systemd-служба:

```text
infra-manager-openbao-startup-unseal.service
```

Её шаблон находится в:

```text
rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2
```

Ansible устанавливает его как:

```text
/etc/systemd/system/infra-manager-openbao-startup-unseal.service
```

Служба запускает:

```text
/usr/local/sbin/infra-manager-openbao-startup-unseal <PVE-узел>
```

### 9.1. Логика запуска

Команда:

1. до 120 секунд ждёт `/v1/sys/seal-status`;
2. если OpenBao не инициализирован — завершает работу без создания нового состояния;
3. если он уже разблокирован — ничего не меняет;
4. если он запечатан — по отдельному root SSH каналу вызывает на PVE:

```text
/usr/local/sbin/infra-manager-openbao-unseal
```

5. PVE использует свой PVE-only unseal key;
6. после unseal PVE материализует рабочие секреты;
7. 910 повторно проверяет `initialized=true` и `sealed=false`.

Проверить службу:

```bash
systemctl status infra-manager-openbao-startup-unseal.service
journalctl -u infra-manager-openbao-startup-unseal.service
```

Запустить проверку вручную:

```bash
systemctl start infra-manager-openbao-startup-unseal.service
```

## 10. Остановка, запуск и обновление

Остановить только OpenBao:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  stop openbao
```

Запустить снова:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d openbao
systemctl start infra-manager-openbao-startup-unseal.service
```

После запуска обязательно подтвердить, что OpenBao не остался запечатанным.

Версия меняется в `provision.yaml`, после чего штатное обновление 910:

1. обновляет `.versions.env`;
2. получает новый образ OpenBao;
3. запускает Compose;
4. выполняет штатную разблокировку;
5. проверяет весь 910.

Ручное удаление `/mnt/persistent-state/openbao` при обновлении запрещено.

## 11. Проверка исправности

Быстрая проверка самого OpenBao:

```bash
curl -fsS http://127.0.0.1:8200/v1/sys/seal-status | jq
```

Ожидаемое рабочее состояние:

```text
initialized: true
sealed:      false
```

Проверить контейнер:

```bash
docker ps --filter name=openbao
docker logs --tail 100 openbao
```

Проверить опубликованные CA:

```bash
test -s /etc/infra-manager/ca/ssh-client-ca.pub
test -s /etc/infra-manager/ca/ssh-host-ca.pub
ssh-keygen -lf /etc/infra-manager/ca/ssh-client-ca.pub
ssh-keygen -lf /etc/infra-manager/ca/ssh-host-ca.pub
```

Полная проектная проверка:

```bash
infra-manager-status --full
```

Она должна подтвердить контейнер, состояние seal, рабочие секреты и PVE-контур.

## 12. Восстановление OpenBao на пересозданном 910

Если LXC 910 потерян, но сохранены постоянные данные, новый OpenBao **не инициализируется заново**.

Практический порядок:

1. создать LXC 910 и подключить прежние `access/` и `state/`;
2. убедиться, что `/mnt/persistent-state/openbao/raft` содержит прежние данные;
3. убедиться, что на PVE сохранён `/mnt/bindmounts/infra-manager/pve-only/openbao/unseal.key`;
4. установить Compose-файлы из `rootfs`;
5. записать `.versions.env` из `provision.yaml`;
6. запустить контейнер `openbao`;
7. выполнить `infra-manager-openbao-startup-unseal` либо запустить соответствующую systemd-службу;
8. проверить `initialized=true`, `sealed=false`;
9. проверить, что рабочие секреты появились в `/run/infra-manager/secrets/`;
10. проверить оба открытых SSH CA;
11. только после этого запускать полную синхронизацию Semaphore и остальные задания;
12. завершить `infra-manager-status --full`.

Если Raft существует, но unseal key утрачен, обычный путь восстановления использовать нельзя.

Если unseal key существует, но Raft утрачен, это также не является основанием автоматически инициализировать пустой OpenBao поверх старой инфраструктуры.

## 13. Типичные неисправности

### 13.1. Контейнер запущен, но `sealed=true`

Проверить:

```bash
systemctl status infra-manager-openbao-startup-unseal.service
journalctl -u infra-manager-openbao-startup-unseal.service
ls -l /mnt/pve-access/pve-host/
```

Затем проверить PVE-only helper и наличие unseal key на PVE.

### 13.2. `initialized=false` после восстановления

Не запускать автоматическую инициализацию. Сначала проверить, действительно ли подключён прежний каталог:

```bash
mountpoint /mnt/persistent-state
find /mnt/persistent-state/openbao -maxdepth 3 -type f -ls
```

Если старый Raft должен существовать, `initialized=false` означает проблему подключения или восстановления состояния.

### 13.3. Нет временных секретов

Проверить:

```bash
ls -la /run/infra-manager/secrets/
infra-manager-status --full
```

Если OpenBao уже разблокирован, повторная PVE-only операция unseal также выполняет материализацию рабочих секретов.

### 13.4. Не опубликован SSH CA

Проверить файлы `/etc/infra-manager/ca/`. Повторный `Initialize OpenBao 910` должен проверять существующие CA и восстанавливать недостающие открытые публикации, не создавая новый CA без необходимости.

## 14. Связанные документы

- [`../provision.yaml`](../provision.yaml) — версия OpenBao, пути и постоянное состояние.
- [`../rootfs/opt/infra-manager/compose/docker-compose.yml`](../rootfs/opt/infra-manager/compose/docker-compose.yml) — контейнер и подключения.
- [`../rootfs/opt/infra-manager/compose/openbao/openbao.hcl`](../rootfs/opt/infra-manager/compose/openbao/openbao.hcl) — listener и Raft.
- [`../rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2`](../rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2) — запуск разблокировки.
- [`data-and-access.md`](data-and-access.md) — физические каталоги и доступы 910.
- [`system-services.md`](system-services.md) — systemd и служебные команды.
- [`../../../../scripts/infra-manager/jobs/initialize-openbao.py`](../../../../scripts/infra-manager/jobs/initialize-openbao.py) — точка входа задания инициализации.
- [`../../../../scripts/infra-manager/host/openbao-unseal.py`](../../../../scripts/infra-manager/host/openbao-unseal.py) — фактическая PVE-only реализация инициализации и unseal.
- [`../../../../docs/700-security/740-openbao.md`](../../../../docs/700-security/740-openbao.md) — роль и контракты OpenBao в проекте.
- [`../../../../docs/800-operations/830-recovery.md`](../../../../docs/800-operations/830-recovery.md) — общий аварийный процесс.