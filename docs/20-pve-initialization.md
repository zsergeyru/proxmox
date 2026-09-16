# Инициализация нового Proxmox VE host

## Статус решения

Bootstrap разделён на две стадии:

```text
Stage 0 — PUBLIC
zsergeyru/proxmox-bootstrap/init-pve.sh
STAGE0_VERSION=4

Stage 1 — PRIVATE
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
BOOTSTRAP_VERSION=12
```

Stage 0 — минимальный zero-day loader при первом запуске и безопасный updater/handoff при последующих запусках. Полная инфраструктурная логика находится только в private Stage 1.

## 1. Исходное состояние

Ожидается новый или частично настроенный Proxmox VE host без обязательного наличия:

- private Git checkout;
- AI control plane;
- Proximo;
- project PVE users/tokens/ACL;
- PVE guest SSH identity;
- Debian VM template `9000`;
- подготовленного Debian 13 LXC appliance.

## 2. Public Stage 0

Активная точка входа:

```text
zsergeyru/proxmox-bootstrap/init-pve.sh
```

### Первый запуск

Назначение Stage 0 при отсутствии `stage0-complete`:

```text
проверить root + Proxmox
→ exclusive lock
→ установить минимальный Git/SSH toolset
→ проверить DNS/HTTPS GitHub
→ создать/переиспользовать temporary read-only Deploy Key
→ проверить доступ к private repo/main
→ shallow checkout
→ передать управление private Stage 1
→ после успеха удалить temporary area
→ создать stage0-complete
```

Stage 0 не содержит внутренние PVE roles, pools, guest model, template builder или AI policy.

### Temporary area

До успешного handoff:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

После успешной Stage 1 эта область удаляется целиком.

Постоянный marker:

```text
/var/lib/proxmox-deployer/state/stage0-complete
```

### Первичная GitHub authorization

Если Deploy Key ещё не зарегистрирован, Stage 0 показывает public half и предлагает добавить его в:

```text
zsergeyru/proxmox
Settings → Deploy keys
Allow write access = OFF
```

После подтверждения Stage 0 повторно проверяет доступ и продолжает тот же run. Private key не передаётся через Git.

### Повторный запуск после stage0-complete

Начиная с Public Stage 0 v4 повторный запуск той же curl-команды не завершается сразу и не создаёт новый keypair.

Вместо этого public entrypoint использует уже созданную private Stage 1 постоянную инфраструктуру:

```text
Linux user pvedeploy
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
/var/lib/proxmox-deployer/repo
```

Алгоритм:

```text
проверить постоянный runtime
→ проверить origin canonical checkout
→ проверить read-only доступ к zsergeyru/proxmox/main
→ fetch main
→ reset canonical checkout на FETCH_HEAD
→ clean project checkout
→ проверить наличие private init
→ запустить уже обновлённую private Stage 1
```

Git-команды выполняются от имени `pvedeploy`. Public bootstrap не создаёт новый постоянный Deploy Key, не ротирует credential и не делает новый clone при повреждённой permanent infrastructure: такой случай приводит к STOP и явному recovery.

Единая штатная команда первого и последующих запусков:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/init-pve.sh | bash
```

С полным обновлением системы:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/init-pve.sh | bash -s -- --update-system
```

## 3. Private Stage 1

Canonical executable:

```text
scripts/pve/bootstrap/init-pve.sh
```

Stage 1 принимает handoff от Stage 0 и затем автономно настраивает host.

Основные обязанности:

```text
host preflight
configuration snapshot
APT/PVE/Ceph repository policy
packages
DNS/time/outbound checks
storage/content types
Debian 13 LXC appliance
pvedeploy + config.yaml
PVE guest SSH identity
canonical GitHub Deploy Key
canonical private checkout
managed pool
PVE roles/users/tokens/ACL
API credential verification
protected Debian template 9000 v6
stable local commands
state/reporting
```

Stage 1 не читает `guest.yaml`/`guests/defaults.yaml` для host bootstrap decisions.

## 4. Target state

После успешной Stage 1:

```text
PVE
├── PVE 9 / Debian trixie baseline
├── pve-no-subscription repository
├── согласованный Ceph no-subscription channel при необходимости
├── local + local-lvm с нужными content types
├── актуальный Debian 13 LXC appliance в local:vztmpl
├── Linux user pvedeploy
├── canonical read-only GitHub Deploy Key
├── PVE guest SSH keypair
├── canonical private checkout
├── pool managed
├── deployer@pve!host-deploy
├── ai-agent@pve!infra
├── локальные API token secrets
├── template 9000 tpl-debian13 Template-Version 6
├── bootstrap/runtime state
└── health/status tooling
```

Host-side deploy работает независимо от AI control plane.

## 5. Preflight

Stage 1 проверяет:

- запуск от root;
- Proxmox VE 9.x;
- Debian trixie;
- KVM;
- `vmbr0`;
- `local` и `local-lvm`;
- конфликт VMID 9000;
- совместимость уже существующего template 9000;
- обязательные PVE CLI tools;
- DNS/outbound access.

Неизвестный VMID 9000 не перезаписывается.

### Existing template compatibility

Если `9000` уже существует, до дальнейшей работы Stage 1 требует:

```text
name = tpl-debian13
template = 1
ciuser = root
description содержит template-version=6
```

Старый Template-Version 4 с `ops` считается несовместимым.

В этом случае:

```text
STOP
→ не удалять template
→ не менять его автоматически
→ потребовать отдельную осознанную пересборку 9000
```

## 6. Configuration snapshot

Перед значимыми host-side изменениями создаётся:

```text
/var/backups/proxmox-bootstrap/YYYYMMDD-HHMMSS/
```

Сохраняются применимые network/hostname/resolver/APT/PVE config files и diagnostics: version, storage, users, roles, ACL, pools, routes и исходный `qm config 9000`.

Это не заменяет VM backup или disaster recovery.

## 7. Repository policy

Основной PVE channel без subscription:

```text
http://download.proxmox.com/debian/pve
trixie
pve-no-subscription
```

Enterprise PVE repo отключается.

Ceph configuration меняется консервативно:

- отсутствует → не создавать;
- уже no-subscription → оставить;
- стандартный enterprise stanza → сохранить тот же `ceph-*` release и переключить channel;
- legacy/multi-stanza/нестандартный вариант → STOP.

`--update-system` отдельно включает `apt full-upgrade`; обычный bootstrap его не навязывает.

## 8. Packages

Stage 1 обеспечивает минимальный runtime:

```text
git
openssh-client
python3
python3-yaml
curl
jq
ca-certificates
```

и небольшой host-admin набор:

```text
mc
htop
tmux
smartmontools
lm-sensors
```

Docker, AI runtimes и application services на PVE host не устанавливаются.

## 9. Storage

Базовые storage:

```text
local
local-lvm
```

Additive-only policy обеспечивает:

```text
local:
  snippets
  vztmpl

local-lvm:
  images
  rootdir
```

Существующие дополнительные content types не удаляются.

## 10. Debian 13 LXC appliance

Stage 1 выполняет runtime discovery:

```text
pveam update
→ найти newest debian-13-standard_*_amd64 archive
→ проверить local:vztmpl
→ скачать при отсутствии
→ verify
```

Точное имя archive не хардкодится в guest defaults. Guest contract хранит только family selector:

```text
local:vztmpl/debian-13-standard
```

Таким образом PVE host заранее подготавливает appliance без необходимости выдавать AI право `Datastore.AllocateTemplate`.

## 11. Runtime filesystem

Canonical structure:

```text
/etc/proxmox-deployer/
├── config.yaml
├── ssh/
│   ├── github_proxmox_repo_ed25519(.pub)
│   ├── pve_guest_ed25519(.pub)
│   ├── config
│   └── known_hosts
└── secrets/
    ├── host-deploy.token
    └── ai-agent-infra.token

/var/lib/proxmox-deployer/
├── repo/
├── state/
└── cache/
```

Подробности: [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md).

## 12. PVE guest SSH identity

Stage 1 один раз создаёт:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Это **host-side** guest-management identity, не GitHub key и не AI key.

Policy:

```text
оба файла отсутствуют
→ создать keypair

private существует
→ не ротировать
→ восстановить public half

private отсутствует, public существует
→ STOP
→ recovery/explicit rotation
```

Private key принадлежит `pvedeploy`, не выводится в logs и должен резервироваться.

## 13. Pool `managed`

Stage 1 создаёт:

```text
managed
```

Это PVE organization/security boundary для AI Proxmox operations:

```text
guest в managed
→ AI получает предусмотренные PVE guest-level permissions

guest вне managed
→ permissions через /pool/managed не распространяются
```

Важно: pool membership **не управляет SSH authorized_keys внутри guest OS**. AI direct SSH является отдельной системой доступа.

## 14. PVE identities

### Host-side / человек

```text
user:  deployer@pve
token: deployer@pve!host-deploy
```

Назначение:

```text
человек
host-side tooling
deploy-guest
```

Guest-level ACL выдаётся на `/vms`, поэтому identity может обслуживать все VM/LXC, включая объекты вне `managed`.

### AI / Proximo

```text
user:  ai-agent@pve
token: ai-agent@pve!infra
```

Назначение:

```text
301 AI Control
→ Proximo
→ Proxmox API
```

Основная write-zone — `/pool/managed`. Template 9000 доступен отдельно как clone source.

## 15. Roles

Project roles:

```text
AICloneSource
AIManagedGuest
AINetworkUse
AIStorage
AIManagedPool
```

Policy additive-only:

```text
роль отсутствует
→ создать

не хватает privilege
→ добавить

есть extra privileges
→ сохранить и при опасных extras предупредить
```

Bootstrap не выполняет автоматическое destructive tightening существующих ролей/ACL.

Полный набор privileges: [`25-pve-access-control.md`](25-pve-access-control.md).

## 16. API tokens

Новый token создаётся с `privsep=1`.

Если token уже существует:

- missing local secret → STOP;
- disabled PVE user → STOP;
- `privsep=0` → warning, без silent conversion;
- real token authentication обязательно проверяется через локальный PVE API.

Host token secret доступен `root:pvedeploy`; AI token secret остаётся root-only.

## 17. Private Git checkout

Canonical checkout:

```text
/var/lib/proxmox-deployer/repo
```

Existing checkout проверяется на ожидаемый origin до fetch/reset. Неожиданный origin → STOP.

GitHub Deploy Key является read-only и отдельным от guest-management SSH key.

## 18. Debian VM template 9000

Current builder:

```text
scripts/pve/create-template.sh
```

Target:

```text
VMID: 9000
name: tpl-debian13
Template-Version: 6
ciuser: root
root password: locked
root SSH: key-only
vga: std
serial0: socket
protection: 1
```

Description содержит:

```text
template-version=6
```

Если 9000 отсутствует, Stage 1 проверяет capacity/source и запускает builder. После завершения проверяются template/protection/root/version marker.

## 19. Guest initial SSH model

Initial management access является частью deploy, а не optional bootstrap:

```text
VM:
Full Clone v6
→ Cloud-Init root public keys
→ start
→ verify root SSH

LXC:
create с ssh-public-keys
→ start
→ verify root SSH
```

После этого могут выполняться `base`, `git`, `docker`, `ansible_controller` и другие явно разрешённые capabilities.

Разные субъекты используют разные public keys одного `root` account:

```text
pve_guest_ed25519.pub
ai_control_ed25519.pub
Ansible provisioning key
personal key при необходимости
```

## 20. State и status

State directory:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── stage0-complete
```

Stage 1 v12 записывает также expected `template_version: 6`.

Статусы включают `running`, `failed`, `interrupted`, `partial`, `ready`, `ready-with-warnings`.

## 21. Stable commands

```text
/usr/local/sbin/pve-bootstrap-status
/usr/local/sbin/deploy-guest
```

`deploy-guest` wrapper устанавливается только когда source `scripts/pve/deploy-guest.py` существует. Пока source отсутствует, bootstrap сообщает `partial`.

## 22. Повторный запуск и safety

Штатный повторный запуск начинается с public entrypoint: он сначала обновляет canonical private checkout, затем запускает уже свежую Stage 1. Stage 1 использует собственный exclusive `flock` и рассчитана на rerun.

Автоматически исправляются только однозначные безопасные вещи. Требуют STOP и отдельного решения:

- несовместимый/неизвестный template 9000;
- потерянный private infrastructure key;
- существующий token без local secret;
- disabled project PVE user;
- неожиданный Git origin;
- нестандартная repository configuration.

Главный принцип:

> Public Stage 0 создаёт initial credential/runtime только при zero-day bootstrap. После `stage0-complete` тот же public entrypoint безопасно обновляет проверенный private source of truth и запускает свежую Stage 1; сама Stage 1 воспроизводимо настраивает host, но не выполняет молчаливые destructive migrations credentials или protected template.
