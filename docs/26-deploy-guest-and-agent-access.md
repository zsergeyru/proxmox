# Кто и как управляет Git и гостями Proxmox

**Type:** Overview  
**Status:** Active  
**Source of truth:** No — точные PVE roles, privileges и ACL задаёт [`25-pve-access-control.md`](25-pve-access-control.md).

Этот документ объясняет схему простыми словами: кто обновляет проект на PVE, кто выполняет `deploy-guest`, чем Linux-user `pvedeploy` отличается от Proxmox identity и как отдельно работает AI agent.

## 1. Четыре разных участника

### `root` на PVE

`root` выполняет только host-side действия, для которых действительно нужны административные права:

- запускает `/usr/local/sbin/deploy-guest`;
- обновляет root-owned canonical checkout проекта на PVE;
- использует отдельный GitHub credential PVE;
- передаёт выполнение основной deploy-логики ограниченному Linux-user `pvedeploy`.

Основной `deploy-guest.py` не должен постоянно работать от `root`.

### `pvedeploy` на PVE

`pvedeploy` — Linux service user, под которым выполняется основная host-side deploy-логика.

Он может читать project source и необходимые deployment credentials/runtime, но не может изменять canonical Git checkout или использовать root-only GitHub credential.

Это **не** Proxmox API account.

### `deployer@pve!host-deploy`

Это Proxmox API identity, которой `deploy-guest.py` представляется самому Proxmox.

Она предназначена для человека и host-side tooling. Точный guest-level scope, project roles и ACL описаны только в [`25-pve-access-control.md`](25-pve-access-control.md).

### AI agent / `ai-agent@pve!infra`

AI agent работает отдельно от host-side `deploy-guest`:

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ разрешённые объекты Proxmox
```

Его обычная PVE write-zone — pool `managed`. AI не получает `root` на PVE и не изменяет canonical checkout проекта на PVE.

## 2. Две независимые копии Git

На PVE:

```text
/var/lib/proxmox-deployer/repo
```

Это root-owned canonical checkout для host-side tooling.

В `301-ai-control`:

```text
/opt/ai-control/repos/proxmox
```

Это отдельная рабочая копия AI control plane.

Они не синхронизируются напрямую. Общая точка обмена — GitHub:

```text
AI / человек
→ GitHub
→ PVE при следующем host-side refresh
```

AI может подготовить и отправить изменение проекта в GitHub своей отдельной identity, но это само по себе ничего не меняет на PVE.

## 3. Как работает `deploy-guest`

Оператор запускает:

```bash
deploy-guest 311
```

для PLAN либо:

```bash
deploy-guest 311 --apply
```

для применения.

Высокоуровневый flow:

```text
root
→ взять orchestration lock
→ проверить canonical checkout
→ получить актуальную revision из GitHub
→ зафиксировать точный Git SHA текущего run
→ запустить основную deploy-логику от pvedeploy

pvedeploy
→ прочитать guest desired state
→ построить PLAN
→ обратиться к Proxmox как deployer@pve!host-deploy
→ APPLY при явном запросе
→ verify
```

После handoff текущий run не должен переключаться на другую Git revision.

Точная файловая модель PVE runtime описана в [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md), а guest desired state — в [`30-guest-manifest.md`](30-guest-manifest.md).

## 4. Почему нужен `pvedeploy`

Основной deploy-код работает с Proxmox API, SSH, guest state и проверками. Если весь этот код постоянно выполнялся бы от `root`, ошибка в deployer автоматически получала бы полный host access.

Поэтому разделены две границы:

```text
root
→ только host trust / Git refresh / запуск

pvedeploy
→ основная deploy-программа

Proxmox API identity
→ дополнительно ограничивает допустимые PVE operations
```

`pvedeploy` не должен иметь альтернативный режим запуска старой mutable-копии проекта в обход root-controlled refresh.

## 5. Как AI управляет гостями

Для обычного guest lifecycle AI не вызывает `deploy-guest`:

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ managed
```

В своей разрешённой области AI может создавать, клонировать, конфигурировать, запускать, останавливать и удалять гостей в пределах PVE ACL.

Новый обычный AI-created guest должен сразу попадать в `managed`.

Template `9000` и специальные/protected гости могут находиться вне `managed`; точные исключения и clone permissions задаёт `25-pve-access-control.md`.

## 6. PVE access и SSH access — разные вещи

Наличие гостя в `managed` означает PVE-level access AI через Proximo, но не добавляет автоматически SSH key внутрь Linux.

```text
PVE pool/ACL
→ lifecycle и hardware/config operations

/root/.ssh/authorized_keys
→ direct shell access внутри guest OS
```

Host-side deployer, AI Control и Ansible используют независимые SSH identities одного management user `root`. Подробнее: [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) и [`50-ai-control.md`](50-ai-control.md).

## 7. Что нельзя смешивать

```text
pvedeploy
```

— Linux user на PVE, под которым выполняется программа.

```text
deployer@pve!host-deploy
```

— Proxmox API identity host-side deployer.

```text
ai-agent@pve!infra
```

— отдельная Proxmox API identity AI Control.

Также отдельны друг от друга:

- GitHub credential PVE;
- GitHub credential AI Control;
- host-side guest SSH key;
- AI guest SSH key;
- Ansible provisioning SSH key.

Private credentials одного контура не копируются в другой только ради удобства.

## 8. Краткая схема

```text
                         GitHub
                        /      \
                       /        \
               AI Control       root на PVE
                    |                |
     AI workspace checkout           v
                    |      canonical PVE checkout
                    |                |
                    |          deploy-guest wrapper
                    |                |
                    |            pvedeploy
                    |                |
                    |   deployer@pve!host-deploy
                    |                |
                    |                v
                    |          Proxmox API
                    |
                    └→ Proximo → ai-agent@pve!infra → managed
```

## 9. Где искать точные правила

- [`25-pve-access-control.md`](25-pve-access-control.md) — канонические PVE identities, roles, privileges и ACL;
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — ownership canonical checkout, credentials и runtime;
- [`30-guest-manifest.md`](30-guest-manifest.md) — desired state гостя;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — initial SSH, bootstrap и Ansible handoff;
- [`50-ai-control.md`](50-ai-control.md) — AI Control / Proximo architecture.

Главное правило:

> PVE обновляет свою canonical копию проекта только через root-controlled host flow; основная deploy-логика выполняется от `pvedeploy` и обращается к Proxmox как `deployer@pve!host-deploy`. AI работает отдельно через Proximo и `ai-agent@pve!infra`, не изменяя canonical checkout на PVE.