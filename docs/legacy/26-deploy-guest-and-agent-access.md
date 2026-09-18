# Кто и как управляет Git и гостями Proxmox

**Тип:** Обзор  
**Статус:** Действующий  
**Основной источник:** Нет — точные роли и ACL задаёт [`25-pve-access-control.md`](25-pve-access-control.md), management SSH keys — [`28-management-ssh-keys.md`](28-management-ssh-keys.md), поведение `deploy-guest` — [`31-deploy-guest.md`](31-deploy-guest.md).

Этот документ объясняет схему простыми словами: кто запускает `deploy-guest`, чем Linux-пользователь `pvedeploy` отличается от учётной записи Proxmox, как работает AI и как оба контура получают общий набор открытых SSH-ключей без SSH-доступа AI к PVE.

## 1. Четыре разных участника

### `root` на PVE

`root` выполняет только действия хоста, для которых действительно нужны административные права:

- запускает `/usr/local/sbin/deploy-guest`;
- обновляет доверенную root-owned копию проекта на PVE;
- владеет мастер-копией общего GitHub Deploy Key только для чтения;
- при `management.project_repo_read: true` передаёт этот credential текущему deploy через отдельный FD;
- запускает основную логику deploy от ограниченного `pvedeploy`.

### `pvedeploy` на PVE

`pvedeploy` — служебный Linux-пользователь, под которым выполняется основная логика развёртывания.

Он использует:

```text
PVE API token deployer@pve!host-deploy
SSH private key deployer для root@guest
public-key registry
known_hosts управляемых гостей
```

Это не учётная запись API Proxmox и не root на PVE.

Канонический public-key registry является изменяемым состоянием deploy-контура и принадлежит `pvedeploy`. Для добавления и удаления открытых management keys отдельный привилегированный helper не нужен.

### `deployer@pve!host-deploy`

Это PVE API identity, от имени которой `deploy-guest.py` управляет VM/LXC.

### AI-агент / `ai-agent@pve!infra`

AI работает отдельно:

```text
AI-агент
→ Proximo
→ ai-agent@pve!infra
→ разрешённые объекты Proxmox
```

Его обычная зона изменения PVE — `managed`. AI не получает root/SSH на PVE ради управления гостями или management public keys.

## 2. Три разных вида доступа

Нельзя смешивать:

```text
PVE API access
→ создание/изменение/жизненный цикл VM/LXC

management SSH access
→ root внутри Debian-гостя

Project Git READ
→ clone/fetch zsergeyru/proxmox
```

Для них используются разные credentials и разные правила. В `guest.yaml` связанные с управлением гостем параметры собраны в одном логическом разделе `management`.

## 3. SSH identity deployer

На PVE существует пара:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Несмотря на историческое имя, это SSH identity **deployer**, а не «ключ самого PVE».

Private key остаётся на PVE. Public часть входит в канонический management public-key registry.

Проект вводится на новом PVE-хосте, поэтому отдельная миграция старой deployer identity сейчас не требуется. Если пары ещё нет, PVE Configuration создаёт её по текущей схеме. После первого создания эта пара считается постоянной и не заменяется автоматически при обычных повторных запусках.

## 4. Канонический management public-key registry

На PVE:

```text
/var/lib/proxmox-deployer/public-keys/
├── deployer.pub
├── <VMID>.pub
└── management-authorized-keys
```

Там только открытые SSH-ключи. Private keys и Git credentials туда не попадают.

Каталог является постоянным изменяемым состоянием deploy-контура и принадлежит `pvedeploy:pvedeploy`. Это сознательное решение: `pvedeploy` уже обладает штатным административным SSH-доступом к управляемым гостям через deployer identity, поэтому root-only каталог открытых ключей не создавал бы отдельной полезной границы безопасности, но потребовал бы лишнего привилегированного посредника.

Имя файла управляющей identity зависит только от VMID. Переименование гостя не переименовывает `.pub` и не создаёт новую identity.

Если конкретному управляющему гостю нужна собственная SSH identity, целевой manifest содержит:

```yaml
management:
  ssh_identity: true
```

После проверенного SSH deployer создаёт/проверяет стандартную пару **внутри этого гостя**, оставляет private key там и забирает только `.pub` в PVE registry.

Это общий механизм: `deploy-guest` не знает, является ли гость AI, Ansible или будущим контроллером.

## 5. Отдельная синхронизация public keys

Отдельная команда на PVE:

```bash
sync-management-keys
```

берёт канонический registry и распространяет его по управляемым Debian-гостям с техническим PVE tag:

```text
management-ssh
```

Этот tag является производным runtime-маркером, а не отдельным полем `guest.yaml`.

Для гостя, развёрнутого через `deploy-guest`, tag устанавливается, если его effective state содержит `management.ssh`. Управляющий гость, создающий Debian VM/LXC напрямую через PVE API, также ставит `management-ssh`, если новая машина входит в management SSH-контур и получает `management-authorized-keys`.

Каждый такой гость получает локальную копию:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и актуальный управляемый блок `root` `authorized_keys`.

`deploy-guest` вызывает sync после регистрации нового/изменённого public key. Оператор также может запускать sync вручную.

Наличие `management-ssh` является необходимым условием участия, но не отменяет проверку типа объекта, ожидаемого адреса, сохранённого SSH-ключа сервера и management-контракта перед записью.

## 6. Почему AI не нужен доступ к PVE для ключей

Направление синхронизации только такое:

```text
PVE canonical registry
→ sync-management-keys
→ 301 AI Control
```

AI не пишет public key в файловую систему PVE и не подключается к PVE по SSH.

Когда появляется, например, management key 311:

```text
deploy-guest 311
→ private key создаётся внутри 311
→ deployer получает только 311 .pub
→ /var/lib/proxmox-deployer/public-keys/311.pub обновляется
→ sync-management-keys
→ 301 получает новый public key 311
```

311 не подключается ни к PVE, ни к 301 ради регистрации.

## 7. Как deployer создаёт новую Debian VM/LXC

При создании:

```text
deploy-guest
→ берёт текущий management-authorized-keys
→ передаёт весь набор через Cloud-Init/LXC ssh-public-keys
→ ставит PVE tag management-ssh
→ запускает guest
→ при первом SSH-подключении запоминает SSH-ключ сервера для ожидаемого адреса
→ повторно подключается уже со строгой проверкой сохранённого ключа
→ проверяет root SSH private key deployer
→ выполняет остальные явно запрошенные шаги
```

Поэтому новая машина сразу доступна всем уже зарегистрированным management identities и однозначно входит в область последующего `sync-management-keys`.

Для уже известного гостя неожиданная смена SSH-ключа сервера означает остановку автоматической работы. Старая запись не удаляется автоматически.

## 8. Как AI создаёт новую Debian VM/LXC

AI использует локальную копию того же набора:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

Схема:

```text
AI
→ прочитать local management-authorized-keys
→ Proximo
→ создать/клонировать guest в managed
→ передать ВЕСЬ набор public keys
→ поставить PVE tag management-ssh
→ запустить
```

Ключ deployer обязательно присутствует в наборе, поэтому PVE-side `sync-management-keys` сможет работать и с AI-created машиной.

При первом PVE-side SSH-подключении к такой новой машине её SSH-ключ сервера принимается и сохраняется для ожидаемого адреса в доверенной домашней сети. После этого действует строгая проверка сохранённого ключа.

AI штатно не вызывает `/usr/local/sbin/deploy-guest` на PVE.

## 9. Git остаётся отдельным credential-контуром, но входит в `management`

На PVE:

```text
/var/lib/proxmox-deployer/repo
```

В AI Control:

```text
/opt/ai-control/repos/proxmox
```

Для `clone/fetch` принят один общий GitHub Deploy Key READ ONLY. Его мастер-копия хранится root-only на PVE, а гостевая копия материализуется только по:

```yaml
management:
  project_repo_read: true
```

Стандартное место в госте:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

Этот Git key не входит в `authorized_keys` и не входит в management public-key registry. Помещение запроса в общий раздел `management` объединяет интерфейс манифеста, но не смешивает сами credentials.

## 10. Что нельзя смешивать

```text
pvedeploy
→ Linux-пользователь на PVE

deployer@pve!host-deploy
→ PVE API identity deployer

pve_guest_ed25519
→ private SSH identity deployer для гостевых ОС

ai-agent@pve!infra
→ PVE API identity AI

management.ssh
→ как проект административно входит в конкретного гостя

management.ssh_identity: true
→ запрос собственной SSH identity конкретного гостя

management.project_repo_read: true
→ только read-only Project Git credential

management-ssh
→ технический PVE tag участия гостя в sync-management-keys

sync-management-keys
→ массовое распространение только public management keys
```

## 11. Жизненный цикл management identity

Для собственной management identity гостя действует простой контракт:

```text
первое создание VM/LXC     → создать пару и зарегистрировать <VMID>.pub
повторный deploy           → сохранить существующую пару
переименование гостя       → ничего с ключом не менять
удаление гостя             → удалить <VMID>.pub и синхронизировать общий набор
новый гость с тем же VMID  → создать новую identity, старый ключ не переиспользовать
ротация                    → только отдельным явным действием
```

Поскольку `deploy-guest` v1 сам не удаляет VM/LXC, удаление гостя с собственной management identity считается завершённым только после удаления соответствующего `<VMID>.pub` и успешного `sync-management-keys`.

## 12. Где искать точные правила

- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — точные пути и владельцы;
- [`23-security.md`](23-security.md) — политика безопасности;
- [`25-pve-access-control.md`](25-pve-access-control.md) — PVE identities, роли и ACL;
- [`28-management-ssh-keys.md`](28-management-ssh-keys.md) — полный lifecycle management keys и tag `management-ssh`;
- [`30-guest-manifest.md`](30-guest-manifest.md) — единый раздел `management`;
- [`31-deploy-guest.md`](31-deploy-guest.md) — PLAN/APPLY;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — Guest Bootstrap;
- [`50-ai-control.md`](50-ai-control.md) — AI Control.

Главное правило:

> Deployer и AI создают VM/LXC разными PVE-контурами, но новые Debian-гости получают один и тот же актуальный набор management public keys и tag `management-ssh`. Канонический набор хранится в `/var/lib/proxmox-deployer/public-keys/`, изменяется `pvedeploy` без отдельного привилегированного helper и распространяется отдельным `sync-management-keys`; private keys остаются у своих владельцев.