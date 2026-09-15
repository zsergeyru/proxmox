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

Bootstrap использует следующие проектные роли.

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

Минимальный набор privileges, который bootstrap должен обеспечить в этой роли:

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

Назначение:

```text
широкое управление обычными VM/LXC в managed
```

Эта роль позволяет automation создавать/удалять и администрировать guest-level объекты в разрешённой зоне.

Если существующая `AIManagedGuest` содержит дополнительные privileges, bootstrap их автоматически не удаляет.

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

## `AIManagedPool`

```text
Pool.Allocate
Pool.Audit
```

Назначение:

```text
работать с resource pool managed
и управлять membership в рамках разрешённой Proxmox RBAC-модели
```

Роль назначается на `/pool/managed`.

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

`init-pve.sh` обязан учитывать, что PVE может быть уже частично настроен и проектные роли могут существовать до запуска bootstrap.

Основной принцип:

> Bootstrap может автоматически **расширить** проектную роль недостающими privileges, но никогда автоматически не **сужает** существующую роль.

## Роль отсутствует

```text
роль отсутствует
→ создать роль с требуемым набором privileges
→ продолжить bootstrap
```

## Роль существует и уже содержит всё необходимое

```text
роль существует
+ все требуемые privileges уже присутствуют
→ использовать существующую роль
→ ничего не удалять
→ продолжить bootstrap
```

Если в роли при этом есть дополнительные privileges, они сохраняются без изменения.

## Роли не хватает privileges

```text
роль существует
+ часть требуемых privileges отсутствует
→ вычислить только missing privileges
→ добавить только их через append
→ существующие privileges не удалять
→ повторно проверить результат
→ продолжить bootstrap
```

Пример:

```text
AIManagedGuest уже содержит:
VM.Allocate
VM.Audit
VM.Config.CPU
...

bootstrap требует дополнительно:
VM.Config.Cloudinit
VM.GuestAgent.Audit

результат:
→ добавить VM.Config.Cloudinit и VM.GuestAgent.Audit
→ все прежние privileges оставить
```

Используется семантика Proxmox `role modify --append 1`, то есть роль не заменяется целиком.

Эта политика позволяет привести старую проектную роль к минимально необходимому состоянию, не ломая уже существующие потребители, которые могут зависеть от дополнительных privileges.

---

# 12. Поведение bootstrap с существующими API-токенами и `privsep`

Для новых API-токенов bootstrap создаёт:

```text
privsep=1
```

Это позволяет отдельно назначать ACL backing user и самому API-токену.

## Токен отсутствует

```text
токен отсутствует
→ создать с privsep=1
→ сохранить одноразовый secret локально
```

## Токен существует с `privsep=1`

```text
→ использовать существующий токен
→ не пересоздавать
→ secret не менять
```

## Токен существует с `privsep=0`

Bootstrap **не должен автоматически менять его на `privsep=1`**.

Причина: такой токен уже может использоваться существующей интеграцией и рассчитывать на полный набор прав backing user. Автоматическое включение privilege separation способно немедленно уменьшить effective permissions токена и сломать работающий сервис.

Поведение:

```text
privsep=0 обнаружен
→ вывести заметное предупреждение
→ объяснить, что токен использует права backing user
→ privsep не менять
→ token secret не менять
→ токен не пересоздавать
→ продолжить bootstrap
```

При этом bootstrap может подготовить желаемые ACL и для самого token principal. Они пригодятся, если перевод на `privsep=1` позднее будет выполнен отдельным осознанным действием человека.

Изменение существующего токена `privsep=0 → privsep=1` выполняется только отдельно после проверки всех текущих потребителей этого токена.

## Токен существует, но локальный secret потерян

Secret существующего Proxmox API-токена нельзя повторно получить из PVE.

Поэтому:

```text
API-токен существует
+ локальный secret-файл отсутствует
→ STOP
→ требуется явная ротация либо восстановление credential
```

Bootstrap не должен молча удалять и пересоздавать такой токен.

---

# 13. Поведение bootstrap с существующими ACL

Применяется неразрушающий принцип:

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

# 14. Итоговая схема

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
