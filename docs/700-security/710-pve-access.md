# Доступ к Proxmox

**Тип:** спецификация  
**Статус:** проектируется  
**Назначение:** определить технические идентичности, роли, ACL и границы доступа к Proxmox после перехода на `910 infra-deployer`.

## 1. Техническая идентичность infra-deployer

Постоянный разворачиватель использует:

```text
infra-deployer@pve!automation
```

Базовый пользователь:

```text
infra-deployer@pve
```

Токен обязательно создаётся с:

```text
privsep=1
```

Пользователь `infra-deployer@pve` не должен состоять в группах Proxmox. Это исключает появление скрытых прав через групповые ACL.

Пользователь и токен получают одинаковый набор прямых ACL. При `privsep=1` фактические права токена являются пересечением прав пользователя и самого токена.

## 2. Управляемая область

Обычные VM/LXC проекта создаются сразу в пуле:

```text
managed
```

Пул `managed` создаёт и проверяет public bootstrap как часть основы управления.

`910 infra-deployer` в `managed` не входит.

OpenTofu не получает `Pool.Allocate`. Поэтому он не создаёт и не удаляет пулы и не используется для произвольного переноса уже существующих чужих VM/LXC в `managed`.

Новый гость должен передавать `pool_id=managed` непосредственно при создании или клонировании.

## 3. Роли

### 3.1. PVEAuditor

Штатная роль:

```text
PVEAuditor
```

назначается на `/` только для чтения состояния PVE, узлов, VM/LXC, хранилищ, сети и пулов.

Она не даёт изменяющих прав.

### 3.2. InfraManagedGuest

Единственная собственная роль новой модели:

```text
InfraManagedGuest
```

Точный набор:

```text
Pool.Audit
VM.Allocate
VM.Audit
VM.Config.CDROM
VM.Config.Cloudinit
VM.Config.CPU
VM.Config.Disk
VM.Config.HWType
VM.Config.Memory
VM.Config.Network
VM.Config.Options
VM.GuestAgent.Audit
VM.PowerMgmt
```

Роль назначается только на:

```text
/pool/managed
```

Поэтому права изменения наследуют только члены этого пула.

В роль намеренно не входят:

- `VM.Backup`;
- `VM.Clone`;
- `VM.Console`;
- `VM.GuestAgent.Unrestricted`;
- `VM.Migrate`;
- `VM.Replicate`;
- `VM.Snapshot` и `VM.Snapshot.Rollback`;
- `Pool.Allocate`.

### 3.3. PVETemplateUser

На источник клонирования:

```text
/vms/9000
```

назначается штатная роль:

```text
PVETemplateUser
```

Она даёт только:

```text
VM.Audit
VM.Clone
```

и не позволяет изменять шаблон.

### 3.4. PVEDatastoreUser

На:

```text
/storage/local-lvm
```

назначается штатная роль:

```text
PVEDatastoreUser
```

Она даёт:

```text
Datastore.Audit
Datastore.AllocateSpace
```

`local` остаётся только читаемым через `PVEAuditor`; выделять на нём место для дисков разворачиватель не может.

### 3.5. PVESDNUser

На:

```text
/sdn/zones/localnetwork/vmbr0
```

назначается штатная роль:

```text
PVESDNUser
```

Она разрешает видеть и использовать `vmbr0`, но не менять конфигурацию сети PVE.

## 4. Полная схема ACL

Одинаково для:

```text
infra-deployer@pve
infra-deployer@pve!automation
```

применяется:

| Путь | Роль | Назначение |
|---|---|---|
| `/` | `PVEAuditor` | только чтение PVE |
| `/pool/managed` | `InfraManagedGuest` | жизненный цикл обычных гостей |
| `/vms/9000` | `PVETemplateUser` | чтение и клонирование шаблона |
| `/storage/local-lvm` | `PVEDatastoreUser` | размещение дисков |
| `/sdn/zones/localnetwork/vmbr0` | `PVESDNUser` | использование основной сети |

Другие прямые ACL для этой идентичности считаются отклонением от контракта и блокируют проверку.

## 5. Что разрешено

`infra-deployer@pve!automation` может:

- читать состояние PVE;
- создавать VM/LXC сразу в `managed`;
- клонировать VM из шаблона `9000` сразу в `managed`;
- изменять CPU, RAM, диски, сеть и обычные параметры членов `managed`;
- работать с cloud-init членов `managed`;
- запускать, останавливать и перезагружать членов `managed`;
- читать состояние QEMU Guest Agent;
- выделять место на `local-lvm`;
- использовать `vmbr0`.

## 6. Что запрещено

Токен не получает:

- права изменения `910`;
- изменяющие права на общем `/vms`;
- `Permissions.Modify`;
- `User.Modify`;
- `Group.Allocate`;
- `Realm.Allocate` и `Realm.AllocateUser`;
- `Sys.Modify`;
- `Sys.PowerMgmt`;
- `Datastore.Allocate` и `Datastore.AllocateTemplate`;
- `SDN.Allocate`;
- `Mapping.Modify`;
- `Pool.Allocate`;
- консоль PVE или VM;
- право менять собственный токен, ACL или роли.

Bootstrap отдельно проверяет фактические effective permissions на `/vms/910`; наличие любого изменяющего VM-права там является ошибкой.

## 7. Ограничение LXC feature flags

Обычный API-токен может управлять безопасными параметрами unprivileged LXC и использовать `nesting` в рамках `VM.Allocate`.

Некоторые feature-флаги LXC, включая `keyctl`, Proxmox разрешает изменять только `root@pam`. Расширение ACL токена это ограничение не снимает.

Поэтому обычный OpenTofu-контур не должен рассчитывать на создание LXC с `keyctl=1`.

`910` является отдельным исключением: его создаёт public bootstrap локально от `root`, поэтому необходимые `nesting=1,keyctl=1` устанавливаются именно там.

Если другому гостю действительно понадобится Docker внутри LXC с `keyctl`, это проектируется как отдельное исключение либо такой сервис переносится в VM.

## 8. Packer

Постоянный токен `automation` предназначен для обычных VM/LXC и клонирования готового `9000`.

Сборка, замена или удаление самого шаблона `9000` через Packer не должна расширять этот токен.

Если Packer потребует дополнительные права, для него создаётся отдельная идентичность или отдельный токен с узким назначением.

## 9. Проверка прав

Public bootstrap проверяет:

- существование `managed`;
- отсутствие `910` в `managed`;
- точный состав `InfraManagedGuest`;
- отсутствие групп у `infra-deployer@pve`;
- обязательные ACL пользователя и токена;
- отсутствие любых дополнительных прямых ACL;
- `privsep=1`;
- обязательные effective permissions на `managed`, `9000`, `local-lvm` и `vmbr0`;
- отсутствие изменяющих прав на `/vms/910`;
- отсутствие административных прав на `/`.

Внутри `910` команда:

```bash
infra-deployer-pve-access-check
```

повторяет проверку effective permissions уже через HTTPS API с реальным токеном и ничего не изменяет.

Для окончательной приёмки OpenTofu дополнительно нужен отдельный тест жизненного цикла временного гостя:

```text
создать в managed
→ изменить CPU/RAM
→ запустить/остановить
→ удалить
```

и отрицательный тест невозможности изменения `910`.

## 10. Секрет токена

Secret `infra-deployer@pve!automation` не хранится в Git и не хранится постоянно на PVE.

Первоначальная передача:

```text
PVE
→ временный root-only файл
→ 910
→ /etc/infra-deployer/secrets/pve-api.env
→ Semaphore Key Store проекта Proxmox Infrastructure
```

На PVE временный файл удаляется после передачи.

## 11. AI

`ai-agent@pve!infra` остаётся отдельной технической идентичностью и не использует credential `infra-deployer`.

Её права проектируются отдельно и не расширяют права `910`.

## 12. Связанные документы

- [`700-overview.md`](700-overview.md) — общая модель безопасности.
- [`720-ssh-access.md`](720-ssh-access.md) — SSH-доступ.
- [`../200-pve/210-host-bootstrap.md`](../200-pve/210-host-bootstrap.md) — создание идентичности.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — конкретный `910`.
- [`../800-operations/810-deployment.md`](../800-operations/810-deployment.md) — процесс развёртывания.