# PVE access control

**Type:** Policy / Reference  
**Status:** Active  
**Source of truth:** Yes — для PVE identities, project roles, ACL boundary и bootstrap reconciliation.

Короткое объяснение operational flow находится в [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md). Здесь фиксируется только технический контракт Proxmox access control.

## 1. Актуальные identities

```text
deployer@pve!host-deploy
ai-agent@pve!infra
```

Назначение:

```text
deployer@pve!host-deploy
→ человек / host-side tooling / deploy-guest
→ guest-level scope: /vms

ai-agent@pve!infra
→ AI Control / Proximo
→ guest-level write-zone: /pool/managed
→ отдельный clone access к template 9000
```

ADR в `guests/320-ai-control/` относятся к bootstrap/legacy-контуру `320` и не являются source of truth для этой модели.

## 2. Security boundaries

### Host-side deployer

`deployer@pve!host-deploy` получает guest-level права на:

```text
/vms
```

с наследованием на все VM/LXC. Это позволяет `deploy-guest` работать с обычными managed guests и со специальными объектами вне `managed`.

Широкий `/vms` scope не означает administrative access к самому PVE host.

### AI automation

Основная write-zone AI:

```text
/pool/managed
```

```text
guest в managed
→ разрешённые guest-level AI operations доступны

guest вне managed
→ обычный AI guest-management ACL на него не распространяется
```

Новый обычный guest, создаваемый AI, должен сразу создаваться или клонироваться в `managed`.

PVE pool membership и direct SSH внутри guest — независимые access mechanisms.

## 3. Protected objects и template 9000

По умолчанию не включать в обычную AI write-zone:

```text
100   production HAOS
301   AI control plane
320   bootstrap/legacy AI control
9000  protected template
```

и другие объекты, для которых профильная документация задаёт исключение.

Template `9000` остаётся вне `managed`. AI получает к нему только audit/clone access. Host-side deployer видит template через `/vms`.

`protection=1` является дополнительным механизмом защиты и не заменяет ACL boundary.

## 4. Project roles

### `AICloneSource`

```text
VM.Audit
VM.Clone
```

Назначение: использовать разрешённый guest/template как clone source. Основной объект — `/vms/9000`.

### `AIManagedGuest`

Минимальный обязательный privilege set:

```text
VM.Allocate
VM.Audit
VM.Backup
VM.Clone
VM.Config.CDROM
VM.Config.Cloudinit
VM.Config.CPU
VM.Config.Disk
VM.Config.HWType
VM.Config.Memory
VM.Config.Network
VM.Config.Options
VM.Console
VM.GuestAgent.Audit
VM.PowerMgmt
VM.Snapshot
VM.Snapshot.Rollback
```

Область определяется ACL path:

```text
deployer@pve!host-deploy
→ AIManagedGuest на /vms

ai-agent@pve!infra
→ AIManagedGuest на /pool/managed
```

`VM.Console` фактически даёт административный доступ внутрь guest через Proxmox Console и выдаётся только доверенным project identities.

### `AINetworkUse`

```text
SDN.Use
```

Разрешает использовать авторизованную guest network/bridge. Не даёт права администрировать host network или SDN infrastructure.

### `AIStorage`

```text
Datastore.AllocateSpace
Datastore.Audit
```

Разрешает использовать авторизованный storage для guest disks/clone. Не даёт права менять storage definitions.

### `AIManagedPool`

```text
Pool.Allocate
Pool.Audit
```

Разрешает работать с pool `managed` и membership объектов в пределах Proxmox RBAC.

## 5. Каноническая ACL matrix

### `deployer@pve!host-deploy`

```text
/vms
→ AIManagedGuest

/pool/managed
→ AIManagedPool

authorized storage
→ AIStorage

authorized network
→ AINetworkUse
```

Отдельный `AICloneSource` на `/vms/9000` deployer не нужен: `VM.Audit` и `VM.Clone` уже входят в `AIManagedGuest` на `/vms`.

### `ai-agent@pve!infra`

```text
/pool/managed
→ AIManagedGuest + AIManagedPool

/vms/9000
→ AICloneSource

authorized storage
→ AIStorage

authorized network
→ AINetworkUse
```

AI identity не получает обычный guest-management ACL на весь `/vms` или `/`.

## 6. Разрешённый AI lifecycle

Внутри разрешённой зоны AI может выполнять guest-level операции, поддерживаемые Proximo и выданными privileges:

- create VM/LXC;
- clone;
- CPU/RAM/disk/network/Cloud-Init configuration;
- start/stop/reboot/shutdown;
- snapshot и rollback;
- backup;
- guest configuration changes;
- delete;
- status/diagnostics и Guest Agent audit.

Нормальный flow:

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ create/clone сразу с pool=managed
→ дальнейшее управление в managed
```

AI не обязан использовать host-side `deploy-guest` для обычного guest lifecycle.

## 7. Что project identities не администрируют

Без отдельного решения не выдавать им права на:

- PVE users/groups/realms;
- roles, ACL и API-token administration;
- host network, physical NIC, routes;
- изменение storage definitions;
- SDN infrastructure administration;
- repository/update policy;
- certificates/ACME;
- firewall самого PVE;
- reboot/shutdown физического PVE;
- самостоятельное расширение собственного уровня доступа.

Широкий guest-level scope deployer не превращает его в host administrator.

## 8. Bootstrap reconciliation

Project RBAC применяется **additive-only**.

### Roles

```text
роль отсутствует
→ создать с обязательным privilege set

роль существует и required privileges присутствуют
→ оставить как есть

роль существует, required privileges неполны
→ вычислить missing privileges
→ append только missing set
→ повторно проверить
```

Используется семантика `role modify --append 1`. Дополнительные существующие privileges автоматически не удаляются.

### API tokens

Новые project tokens создаются с:

```text
privsep=1
```

Существующий `privsep=1` не пересоздаётся и его secret не меняется.

Существующий `privsep=0` автоматически не переводится на `privsep=1`:

```text
privsep=0
→ WARNING
→ token оставить без изменения
→ desired token ACL можно подготовить
```

Переход на `privsep=1` выполняется отдельным осознанным действием после проверки текущих потребителей.

Если token существует, но canonical local secret потерян:

```text
→ STOP
→ требуется явное recovery/rotation решение
```

Bootstrap не удаляет и не пересоздаёт такой token молча.

Детали безопасного сохранения нового one-time secret и rollback описаны в [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md).

### ACL

```text
required ACL существует
→ оставить

required ACL отсутствует
→ добавить

дополнительные legacy ACL существуют
→ не удалять автоматически
→ при необходимости warning/report
```

Сужение roles/ACL выполняется только отдельной операцией после проверки существующих потребителей.

## 9. Проверка результата

После bootstrap проверяются не только записи конфигурации, но и effective permissions:

```text
project role содержит required privileges
required ACL существуют
API token существует
canonical local credential доступен ожидаемому consumer
pveum user token permissions показывает ожидаемый scope
реальный API request проходит authentication
```

Для AI дополнительно проверяется boundary:

```text
managed guest
→ разрешённые guest-level operations доступны

protected guest вне managed
→ обычные AI guest-management operations недоступны

template 9000
→ audit/clone доступен
→ обычный AI write access отсутствует
```

## 10. Связанные документы

- [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md) — короткая схема участников и operational flow;
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — filesystem/credential ownership на PVE;
- [`23-security.md`](23-security.md) — общие security invariants;
- [`30-guest-manifest.md`](30-guest-manifest.md) — desired state VM/LXC;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — management readiness и provisioning;
- [`50-ai-control.md`](50-ai-control.md) — AI Control architecture.

Главный технический принцип:

> `deployer@pve!host-deploy` — host-side guest-level identity на `/vms`; `ai-agent@pve!infra` — отдельная AI identity с write-zone `/pool/managed` и ограниченным clone access к template `9000`. Bootstrap расширяет project roles/ACL только до требуемого минимума и автоматически не сужает существующий доступ.