# PVE access control

## Статус

Это каноническая политика прав для новой схемы PVE bootstrap/deploy и `301-ai-control`.

Она относится к двум актуальным Proxmox service identities:

```text
deployer@pve!host-deploy
ai-agent@pve!infra
```

У этих identities разное назначение. Их не следует считать двумя взаимозаменяемыми токенами одного назначения.

ADR из `guests/320-ai-control/` описывает существующий bootstrap/legacy-контур `320` и может использоваться как историческая справка, но не является source of truth для новой схемы прав.

---

# 1. Общая модель

Принята простая схема:

```text
человек / локальная host-side automation
→ deployer@pve!host-deploy

AI control / Proximo / AI agents
→ ai-agent@pve!infra
```

Основная граница для AI automation — Proxmox resource pool:

```text
managed
```

AI agent получает широкие guest-level возможности внутри `managed`, включая создание, clone, настройку и удаление VM/LXC.

Объекты вне `managed` не должны автоматически становиться доступны AI agent.

---

# 2. `deployer@pve!host-deploy`

```text
user:  deployer@pve
token: deployer@pve!host-deploy
```

## Назначение

Это инфраструктурная учётная запись для работы **человека или локальной host-side automation непосредственно с Proxmox**.

Она нужна для сценариев, когда управление выполняется не AI agent через Proximo, а непосредственно на стороне PVE или оператором через подготовленные инструменты проекта.

Типичные потребители:

```text
человек
→ deploy-guest
→ deployer@pve!host-deploy

локальный PVE deploy/runtime
→ deployer@pve!host-deploy
```

Эта identity не является учёткой AI agent.

## `deploy-guest`

`deploy-guest` — удобный интерфейс для человека и host-side automation, а не обязательный security gateway для AI agent.

Пример:

```bash
deploy-guest 311 --apply
```

Он должен уметь:

- читать `guests/*/guest.yaml`;
- создавать VM/LXC;
- clone из template `9000`;
- задавать CPU/RAM/disks/network/Cloud-Init и другие guest-level параметры;
- использовать разрешённые storage и bridge;
- запускать/останавливать guest во время deploy и verification;
- помещать обычный guest в `managed`;
- выполнять проверки после deploy.

Таким образом `deploy-guest` остаётся удобным, воспроизводимым способом для человека развернуть guest по Git desired state.

AI agent при этом не обязан использовать `deploy-guest`: для штатной AI automation он работает через собственную identity `ai-agent@pve!infra` и Proximo.

## Граница прав

`deployer@pve!host-deploy` предназначен для guest-level deployment и работы с разрешёнными ресурсами Proxmox.

Он не предназначен для произвольного администрирования самого PVE host, включая IAM/ACL, изменение host network, storage definitions, repositories, certificates и reboot/shutdown физического PVE.

---

# 3. `ai-agent@pve!infra`

```text
user:  ai-agent@pve
token: ai-agent@pve!infra
```

## Назначение

Это отдельная инфраструктурная учётная запись **именно для AI control plane**.

Текущий основной потребитель:

```text
301-ai-control
→ Proximo MCP
→ ai-agent@pve!infra
→ Proxmox API
```

Любой разрешённый AI agent внутри `301` использует Proximo, а Proximo обращается к PVE от имени `ai-agent@pve!infra`.

Эта identity не используется как обычная человеческая учётка для ручной работы.

## Возможности AI agent

В своей разрешённой зоне AI agent может самостоятельно:

- создавать VM;
- создавать LXC;
- clone существующего template;
- задавать CPU/RAM/disks/network/guest options;
- использовать Cloud-Init через поддерживаемые операции Proximo;
- start/stop/reboot/shutdown;
- делать snapshots;
- выполнять rollback;
- запускать backup;
- менять конфигурацию guest;
- удалять VM/LXC;
- выполнять diagnostics/status и разрешённые guest-level операции.

То есть создание и удаление VM/LXC **не требуется проводить через `deploy-guest`**.

---

# 4. Что такое pool `managed`

`managed` — это Proxmox resource pool, который используется одновременно как:

```text
organization boundary
+
security boundary
```

Простыми словами:

> `managed` содержит VM/LXC, которыми AI automation разрешено штатно управлять.

Основная write-zone AI identity:

```text
/pool/managed
```

Логика:

```text
guest находится в managed
→ AI agent получает разрешённые guest-level права

guest находится вне managed
→ AI agent штатно не должен иметь write-доступ к нему
```

Это позволяет не строить сложную систему индивидуальных ACL для каждой обычной VM/LXC.

---

# 5. Создание новых VM/LXC агентом

AI agent разрешено самостоятельно создавать новые VM/LXC.

Основное правило:

> Новый обычный guest, создаваемый AI automation, должен сразу создаваться в pool `managed`.

Для clone Proximo поддерживает параметр pool непосредственно в `pve_clone`:

```text
pve_clone(..., pool="managed")
```

Для создания VM/LXC с нуля Proximo позволяет передать Proxmox create options, включая:

```text
options:
  pool: managed
```

Поэтому нормальный flow выглядит так:

```text
AI agent
→ Proximo
→ create/clone
→ pool=managed
→ новый VM/LXC сразу появляется в managed
```

Не требуется сначала создать guest вне `managed`, а затем отдельной операцией переносить его в pool.

---

# 6. Почему protected VM вне `managed` остаются защищёнными

По умолчанию не включать в обычную AI write-zone:

```text
100   production HAOS
301   AI control plane
320   bootstrap/legacy AI control
9000  protected template
```

И другие объекты, для которых документация явно задаёт исключение.

Например:

```text
VM 100 вне managed
```

AI agent не получает на неё обычные guest-level права через `/pool/managed`.

То есть наличие у AI права работать с pool `managed` не означает автоматически права взять произвольную существующую VM вне pool и начать ею управлять.

Для создания нового объекта agent использует свои allocate/clone права в разрешённом deployment flow и создаёт новый guest сразу в `managed`.

Не выдавать `ai-agent@pve!infra` широкие guest-management ACL на весь `/` или на все `/vms`, потому что это разрушит смысл `managed` как основной security boundary.

---

# 7. Template `9000`

```text
9000  tpl-debian13
```

Template остаётся вне обычной write-zone `managed`.

AI agent должен иметь возможность:

```text
видеть template
→ clone из 9000
→ создать новый VMID сразу в managed
```

Но сам template не должен автоматически становиться обычным managed guest.

Для этого используется отдельный ограниченный доступ к clone source.

---

# 8. Принятые Proxmox roles

На текущем PVE уже существуют роли:

## `AICloneSource`

```text
VM.Audit
VM.Clone
```

Назначение:

```text
разрешить видеть template/source guest
и использовать его как источник clone
```

Основной объект — template `9000`.

## `AIManagedGuest`

```text
VM.Allocate
VM.Audit
VM.Backup
VM.Clone
VM.Config.CPU
VM.Config.Disk
VM.Config.Memory
VM.Config.Network
VM.Config.Options
VM.PowerMgmt
VM.Snapshot
VM.Snapshot.Rollback
```

Назначение:

```text
широкое управление обычными VM/LXC в managed
```

Эта роль позволяет AI automation создавать/удалять и администрировать guest-level объекты в разрешённой зоне.

## `AINetworkUse`

```text
SDN.Use
```

Назначение:

```text
использование разрешённой сети/bridge гостем
```

Это не право администрировать host network или SDN infrastructure.

## `AIStorage`

```text
Datastore.AllocateSpace
Datastore.Audit
```

Назначение:

```text
видеть разрешённый storage
и выделять пространство под guest disks/clone/другие разрешённые guest-level операции
```

Это не право изменять определения Proxmox storage.

## Права на pool

Для работы с `managed` AI identity должны быть выданы необходимые pool-level privileges на сам `/pool/managed`, включая возможность создавать обычные объекты сразу в этом pool и управлять membership в рамках возможностей Proxmox RBAC.

Допускается отдельная небольшая роль для pool-level privileges, если это требуется фактической версией PVE.

Точные privilege names для pool-level роли перед реализацией проверяются на установленной версии Proxmox VE.

---

# 9. Роли для двух identities

## `deployer@pve!host-deploy`

Получает права, достаточные для прямого host-side deployment:

```text
clone source
managed guests
authorized storage
authorized network
managed pool membership
```

Эта identity используется человеком/host-side tooling.

## `ai-agent@pve!infra`

Получает аналогичные guest-level возможности, необходимые AI automation:

```text
clone source
managed guests
authorized storage
authorized network
managed pool membership
```

Но эта identity используется только AI control/Proximo.

Ключевое различие двух identities — не попытка искусственно сделать одну из них значительно слабее другой, а **разделение потребителей и audit trail**:

```text
deployer
→ человек / host-side tools

ai-agent
→ AI / Proximo
```

Credentials могут независимо ротироваться и отзываться.

---

# 10. Что AI identity не должна администрировать

`ai-agent@pve!infra` штатно не предназначена для управления самим гипервизором.

Не выдавать ей без отдельного решения права на:

- PVE users/groups/realms;
- ACL/roles/API tokens;
- host network/physical NIC/routes;
- изменение storage definitions;
- SDN infrastructure administration;
- repositories/update policy;
- certificates/ACME;
- firewall самого PVE;
- reboot/shutdown физического PVE host;
- изменение собственного уровня доступа.

То же общее правило действует и для обычной host-deployer identity: guest deployment не должен автоматически превращаться в полный PVE administrator access.

---

# 11. Поведение bootstrap при уже существующих ролях

Новый `init-pve.sh` обязан учитывать, что PVE может быть уже частично настроен и нужные роли могут существовать до запуска bootstrap.

Скрипт не должен считать сам факт существования роли ошибкой.

## Роль отсутствует

```text
роль отсутствует
→ создать роль с ожидаемым набором privileges
→ продолжить bootstrap
```

Пример:

```text
[CREATE] role AICloneSource
```

## Роль существует и совпадает

```text
роль существует
+ privileges совпадают
→ использовать существующую роль
→ ничего не пересоздавать
→ продолжить bootstrap
```

Пример:

```text
[OK] role AICloneSource already exists and matches
```

## Роль существует, но набор privileges отличается

```text
роль существует
+ privileges отличаются от ожидаемых
→ НЕ перезаписывать автоматически
→ НЕ удалять роль
→ НЕ останавливать bootstrap
→ показать warning
→ показать missing/extra privileges
→ продолжить остальные стадии
```

Пример:

```text
[WARN] role AIManagedGuest already exists and differs
       missing: VM.Config.Cloudinit
       extra:   none
       existing role left unchanged
       bootstrap continues
```

Причина такой политики: существующая роль уже может использоваться текущей инфраструктурой, например legacy/bootstrap `320`, и молчаливое изменение её privileges может нарушить работающий доступ.

Отличие existing role от desired role — это diagnostic warning, а не fatal bootstrap error.

---

# 12. Поведение bootstrap с существующими ACL

Аналогичный принцип применяется к ACL.

```text
нужный ACL существует
→ использовать

нужного ACL нет
→ добавить

существуют дополнительные старые ACL
→ не удалять автоматически
→ при необходимости показать warning/report
→ продолжить bootstrap
```

Bootstrap не должен выполнять массовую очистку существующих ACL только ради приведения PVE к своей модели.

Удаление или сужение старых ACL выполняется отдельным осознанным действием после проверки их текущего использования.

---

# 13. Итоговая схема

## Работа человека

```text
человек
→ deploy-guest или другой host-side tool
→ deployer@pve!host-deploy
→ Proxmox
```

`deploy-guest` — удобный интерфейс для воспроизводимого deployment по `guest.yaml`.

## Работа AI

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ Proxmox
```

AI самостоятельно создаёт, настраивает, обслуживает и удаляет разрешённые VM/LXC.

Для обычных новых объектов:

```text
create/clone
→ сразу managed
→ дальнейшее управление в managed
```

## Главный принцип

> `managed` — рабочая зона AI automation. `deployer@pve!host-deploy` — отдельная identity для человека и прямой host-side работы с Proxmox. `ai-agent@pve!infra` — отдельная identity именно для AI/Proximo. Новые обычные гости AI создаёт сразу в `managed`, а protected guests остаются вне этого pool и не получают автоматически права AI write-access.
