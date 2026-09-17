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
- при `access.project_repo_read` передаёт этот credential текущему deploy через отдельный FD;
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

Для них используются разные credentials и разные правила.

## 3. SSH identity deployer

На PVE существует пара:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Несмотря на историческое имя, это SSH identity **deployer**, а не «ключ самого PVE».

Private key остаётся на PVE. Public часть входит в канонический management public-key registry.

## 4. Канонический management public-key registry

На PVE:

```text
/etc/proxmox-deployer/public-keys/
├── deployer.pub
├── <VMID>-<name>.pub
└── management-authorized-keys
```

Там только открытые SSH-ключи. Private keys и Git credentials туда не попадают.

Если конкретному управляющему гостю нужна собственная SSH identity, целевой manifest содержит:

```yaml
management_key: true
```

После проверенного SSH deployer создаёт/проверяет стандартную пару **внутри этого гостя**, оставляет private key там и забирает только `.pub` в PVE registry.

Это общий механизм: `deploy-guest` не знает, является ли гость AI, Ansible или будущим контроллером.

## 5. Отдельная синхронизация public keys

Отдельная команда на PVE:

```bash
sync-management-keys
```

берёт канонический registry и распространяет его по участвующим управляемым Debian-гостям.

Каждый такой гость получает локальную копию:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и актуальный управляемый блок `root` `authorized_keys`.

`deploy-guest` вызывает sync после регистрации нового/изменённого public key. Оператор также может запускать sync вручную.

## 6. Почему AI не нужен доступ к PVE для ключей

Направление синхронизации только такое:

```text
PVE canonical registry
→ sync-management-keys
→ 301 AI Control
```

AI не пишет public key в `/etc/proxmox-deployer` и не подключается к PVE по SSH.

Когда появляется, например, management key 311:

```text
deploy-guest 311
→ private key создаётся внутри 311
→ deployer получает только 311 .pub
→ PVE registry обновляется
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
→ запускает guest
→ проверяет root SSH private key deployer
→ выполняет остальные явно запрошенные шаги
```

Поэтому новая машина сразу доступна всем уже зарегистрированным management identities.

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
→ запустить
```

Ключ deployer обязательно присутствует в наборе, поэтому PVE-side `sync-management-keys` сможет работать и с AI-created машиной.

AI штатно не вызывает `/usr/local/sbin/deploy-guest` на PVE.

## 9. Git остаётся отдельным контуром

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
access:
  project_repo_read: true
```

Стандартное место в госте:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

Этот Git key не входит в `authorized_keys` и не входит в management public-key registry.

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

management_key: true
→ запрос собственной SSH identity конкретного гостя

sync-management-keys
→ массовое распространение только public management keys

project_repo_read
→ только read-only Project Git credential
```

## 11. Где искать точные правила

- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — точные пути и владельцы;
- [`23-security.md`](23-security.md) — политика безопасности;
- [`25-pve-access-control.md`](25-pve-access-control.md) — PVE identities, роли и ACL;
- [`28-management-ssh-keys.md`](28-management-ssh-keys.md) — полный lifecycle management keys;
- [`30-guest-manifest.md`](30-guest-manifest.md) — целевые `management_key` и `project_repo_read`;
- [`31-deploy-guest.md`](31-deploy-guest.md) — PLAN/APPLY;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — Guest Bootstrap;
- [`50-ai-control.md`](50-ai-control.md) — AI Control.

Главное правило:

> Deployer и AI создают VM/LXC разными PVE-контурами, но новые Debian-гости получают один и тот же актуальный набор management public keys. Канонический набор ведётся на PVE и распространяется отдельным `sync-management-keys`; private keys остаются у своих владельцев.