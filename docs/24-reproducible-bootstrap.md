# Версии и воспроизводимость bootstrap/configuration

**Type:** Policy  
**Status:** Active  
**Source of truth:** Yes — для versioning, source revision, rerun/recovery semantics и reproducibility boundaries проекта.

Этот документ отвечает на вопрос **«какие изменения считаются новым contract и что делает run воспроизводимым»**. Операторские шаги находятся в [`20-pve-initialization.md`](20-pve-initialization.md), filesystem/runtime layout — в [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md), template contract — в [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## 1. Что проект версионирует явно

Проект имеет три независимых contract version:

```text
Public Bootstrap
PVE Configuration
Debian VM template
```

Конкретные текущие значения определяются соответствующим кодом и template passport; вторичные документы не должны копировать их без необходимости.

Версии повышаются только при изменении собственного contract, а не при каждом изменении текста или внешней package version.

## 2. Public Bootstrap version

Версия Public Bootstrap меняется, когда меняется поведение публичной точки входа, например:

- first-run bootstrap;
- resume/retry semantics;
- handoff в private PVE Configuration;
- permanent source refresh;
- orchestration lock contract;
- source trust boundary;
- передаваемые параметры/markers.

Public Bootstrap между отдельными запусками получает актуальный `main`, но один конкретный run обязан зафиксировать одну exact Git revision private source.

## 3. PVE Configuration version

Версия PVE Configuration меняется при изменении host-side contract, например:

- filesystem/runtime layout;
- credential model;
- PVE roles/ACL;
- storage prerequisites;
- state schema;
- template pipeline ownership;
- safety/reconciliation semantics;
- smoke lifecycle.

Она не обязана меняться при правке комментариев, сообщений или документации, не меняющей contract.

## 4. Template version

Template version меняется только при изменении guest-facing contract самого template `9000`.

Примеры:

- management user / SSH policy;
- kernel/console model;
- base guest packages, если это часть обязательного contract;
- Cloud-Init behavior;
- cleanup/seal contract;
- clone-visible behavior.

Изменение только host-side orchestration или smoke implementation не требует автоматически повышать Template-Version.

Канонический template contract: [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## 5. Одна Git revision на один run

Один PVE Configuration run не должен смешивать код из разных Git revisions.

Нормальная модель:

```text
Public Bootstrap выбирает exact private Git revision
→ передаёт SHA в PVE Configuration
→ PVE Configuration проверяет executing source
→ canonical checkout синхронизируется на тот же SHA
→ state фиксирует эту revision
```

Если `main` изменился после начала run, текущий run продолжает работать только со своей выбранной revision либо останавливается при невозможности подтвердить её.

## 6. Root trust boundary

Код, который выполняется от `root`, должен происходить из root-trusted source tree.

Минимальные свойства:

```text
canonical checkout принадлежит root
parent не writable для pvedeploy
repo/.git не writable для pvedeploy
Git credential canonical checkout доступен только root
executing worktree clean
```

Точные пути и permissions задаёт [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md).

`pvedeploy` может читать source для deploy runtime, но не должен изменять код, который затем будет исполнен от `root`.

## 7. Temporary и permanent checkout имеют разную семантику

Temporary first-run checkout является disposable и может очищаться/пересоздаваться.

Permanent canonical checkout является fail-closed:

```text
local drift обнаружен
→ STOP
→ не стирать изменения автоматически
```

Это правило защищает от незаметного уничтожения неизвестного локального состояния и от подмены root-executed source.

## 8. Rerun и idempotency

Повторный запуск должен быть штатным сценарием.

Project automation должна:

- обнаруживать уже существующее корректное состояние;
- повторно использовать его;
- добавлять только недостающее там, где это безопасно;
- не ротировать credentials без отдельного решения;
- не уничтожать неоднозначные VM/LXC/template/smoke objects;
- выполнять verification после reconcile.

`STOP` предпочтительнее destructive guess.

## 9. State и durability

Host-side state должен позволять понять как минимум:

- выполнялся ли run;
- его source revision;
- success/failure/interruption;
- состояние template smoke lifecycle;
- последний успешно применённый source revision.

State files должны записываться атомарно, когда это возможно.

Точные state paths принадлежат [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md).

## 10. API token secret durability

One-time API token secret нельзя получить повторно из PVE после создания.

Поэтому создание token и сохранение его secret рассматриваются как единая recovery-sensitive операция:

```text
создать token
→ считать one-time secret
→ безопасно записать temporary secret file
→ owner/mode
→ atomic move в canonical location
→ считать операцию завершённой
```

Если новый token текущего run создан, но secret сохранить не удалось, automation может откатить именно этот новый pending token.

Уже существующий token не удаляется и не ротируется автоматически.

## 11. Внешние компоненты

Внешние stable-компоненты по умолчанию не pin'ятся до каждой package revision.

Базовый принцип:

```text
trusted stable channel
→ штатная checksum/signature verification, если доступна
→ явная post-install verification
```

Exact pinning или snapshot repository вводятся только там, где есть практическая потребность в более строгой воспроизводимости.

Это относится в том числе к Debian packages и LXC appliance revisions.

## 12. Что считается воспроизводимым результатом

Проект не гарантирует byte-identical host или guest image.

Гарантируется воспроизводимость **contract**:

```text
те же project rules
те же source manifests
совместимый external stable input
→ ожидаемый host/guest contract
→ обязательная verification
```

Source image/package revisions могут отличаться между разными датами сборки, если они удовлетворяют принятой policy.

## 13. CI и real-runtime verification

CI обязана проверять статические contracts настолько, насколько это возможно:

- repository/schema validation;
- Python/shell syntax;
- lint;
- template renderer/schema;
- unit tests safety/state/reconciliation;
- source trust tests;
- отсутствие запрещённых legacy entrypoints.

CI не заменяет real Proxmox runtime.

Реальные host/template/clone properties подтверждаются PVE Configuration и smoke-test согласно template policy.

## 14. Где что искать

| Вопрос | Канонический документ |
|---|---|
| Как запустить bootstrap/configuration | [`20-pve-initialization.md`](20-pve-initialization.md) |
| Где лежат source/state/credentials | [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) |
| PVE identities/ACL | [`25-pve-access-control.md`](25-pve-access-control.md) |
| Template 9000 contract | [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) |
| Backup/recovery | [`22-storage-and-backup.md`](22-storage-and-backup.md), [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md) |

Главное правило: **версионируем собственные contracts, фиксируем одну source revision на run и проверяем результат; не пытаемся превратить всю внешнюю систему в byte-for-byte snapshot без необходимости.**