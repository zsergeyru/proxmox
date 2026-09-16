# Public Bootstrap и PVE Configuration — текущее состояние

## Статус

Проект использует два компонента с разными обязанностями:

```text
PUBLIC BOOTSTRAP
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=7

PVE CONFIGURATION
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=15
```

Канонические документы:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md);
- [`25-pve-access-control.md`](25-pve-access-control.md);
- [`30-guest-manifest.md`](30-guest-manifest.md);
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## Public Bootstrap

Public Bootstrap — минимальная публичная точка входа. Он не содержит внутреннюю модель PVE roles, pools, guest manifests, template implementation или AI policy.

Каноническая команда:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

С явным полным обновлением Proxmox/Debian:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

### Чистый первый запуск

```text
root/PVE check
→ exclusive lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ private repo authorization
→ temporary shallow checkout main
→ определить точный HEAD
→ PVE_CONFIGURATION_SOURCE_REVISION=<HEAD>
→ запуск PVE Configuration
→ cleanup /var/lib/proxmox-bootstrap
→ bootstrap-complete
```

Если Deploy Key ещё не зарегистрирован, Public Bootstrap показывает public half, ждёт подтверждение пользователя и повторно проверяет доступ. Private keys и token secrets через Git не передаются.

### Resume незавершённого первого запуска

Отсутствие `bootstrap-complete` больше не означает, что созданный permanent runtime надо удалять.

Если canonical runtime уже готов для Git refresh:

```text
pvedeploy существует
canonical Deploy Key существует
known_hosts существует
SSH config существует
/var/lib/proxmox-deployer/repo/.git существует
```

Public Bootstrap:

```text
использует существующий credential
→ обновляет canonical checkout
→ запускает текущую PVE Configuration
→ после успешного завершения удаляет оставшийся temporary runtime
→ создаёт bootstrap-complete
```

Если permanent runtime только частично создан, first-run path продолжается через temporary runtime. Существующие permanent credentials не удаляются и не ротируются автоматически.

Это позволяет после ошибки исправить причину и запустить **ту же команду ещё раз**, не теряя одноразовые API token secrets.

### Повторный запуск после завершённого bootstrap

При наличии `bootstrap-complete` выполняется:

```text
проверить permanent runtime
→ проверить origin canonical checkout
→ подтвердить read-only доступ к zsergeyru/proxmox/main
→ fetch main
→ reset --hard FETCH_HEAD
→ clean -ffd
→ определить полученный HEAD
→ передать его в PVE Configuration
```

Повреждённый permanent credential не восстанавливается путём молчаливой ротации.

## Одна private revision на один run

Public Bootstrap выбирает revision до запуска PVE Configuration и передаёт:

```text
PVE_CONFIGURATION_SOURCE_REVISION=<40-char SHA>
```

Дальше весь configuration run привязан к этому SHA.

Если canonical checkout уже находится на ожидаемой revision, PVE Configuration не выполняет лишний fetch/reset. Если checkout отстаёт, допускается fetch только при условии, что `FETCH_HEAD` **точно совпадает** с source revision текущего run. Если `main` успел продвинуться дальше, выполнение останавливается вместо смешивания кода из разных commits.

Фактически применённая revision записывается в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

## PVE Configuration

Canonical executable:

```text
scripts/pve/setup/configure-pve.sh
```

Модульная структура:

```text
scripts/pve/setup/
├── configure-pve.sh
└── lib/
    ├── 00-common.sh
    ├── 10-preflight.sh
    ├── 20-system.sh
    ├── 30-storage.sh
    ├── 40-runtime.sh
    ├── 50-access.sh
    └── 70-template-tooling.sh
```

Основная последовательность:

```text
exclusive configuration lock
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ configuration snapshot
→ PVE/Ceph repository policy
→ packages
→ DNS/time/outbound checks
→ active storage + content types
→ Debian 13 LXC appliance
→ pvedeploy + config.yaml
→ PVE guest SSH identity
→ canonical GitHub Deploy Key
→ canonical private checkout той же source revision
→ managed pool
→ roles/users/API tokens/ACL
→ effective token permission checks
→ token API authentication checks
→ capacity/source checks
→ template 9000
→ local tooling/status
→ final state
```

PVE Configuration не читает `guest.yaml` или `guests/defaults.yaml` как вход для host configuration.

## `--update-system`

Без параметра PVE Configuration может выполнить `apt update` и установить отсутствующие project packages, но не делает полный system upgrade.

Только явный:

```text
--update-system
```

включает `apt full-upgrade`.

## PVE guest SSH identity

PVE Configuration создаёт и сохраняет:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Это host-side management credential для `deploy-guest`, отдельный от GitHub и AI identities.

Поведение:

```text
private + public отсутствуют → создать Ed25519 keypair
private существует → не ротировать, восстановить .pub
private отсутствует, public существует → STOP
```

## LXC readiness

PVE Configuration обеспечивает `local:vztmpl` и выполняет:

```text
pveam update
→ найти newest debian-13-standard_*_amd64 archive
→ скачать его в local, если отсутствует
→ verify
```

Guest contract хранит selector семейства:

```text
local:vztmpl/debian-13-standard
```

## Template 9000

Текущий canonical template contract:

```text
VMID: 9000
name: tpl-debian13
Template-Version: 6
ciuser: root
root password: locked
root SSH: public-key only
protection: 1
```

Builder:

```text
scripts/pve/create-template.sh
```

PVE Configuration теперь использует один полный host-visible contract как для существующего template, так и после новой сборки:

```text
name=tpl-debian13
template=1
agent=1
vga=std
serial0=socket
ciuser=root
ciupgrade=0
ipconfig0=ip=dhcp
cicustom отсутствует
description содержит template-version=6
```

После `ensure_template` дополнительно обязательно `protection=1`.

Если существующий VMID `9000` не соответствует contract, выполняется STOP. Единственное автоматически исправляемое отклонение — отсутствие `protection=1` у в остальном совместимого template после предварительного configuration snapshot.

## Root-only guest management

Template и guest contract используют единый management user:

```text
root:22
```

Root password locked. Password/KbdInteractive authentication выключены.

Initial guest access создаётся штатным механизмом типа гостя:

```text
VM  → Cloud-Init public keys для root
LXC → ssh-public-keys для root
```

## PVE identities и ACL

Используются две PVE identities:

```text
deployer@pve!host-deploy
→ человек / host-side tooling / deploy-guest
→ guest-level доступ на /vms

ai-agent@pve!infra
→ AI / Proximo
→ write-zone /pool/managed
→ template 9000 отдельно как clone source
```

Policy ролей и ACL остаётся additive-only. Новый token создаётся с `privsep=1`; существующий `privsep=0` по принятой политике не меняется автоматически.

Effective permissions API-токенов проверяются штатной PVE-командой:

```text
pveum user token permissions <userid> <tokenid>
```

После этого выполняется реальная token+secret авторизация в локальном Proxmox API.

## Runtime и status

Постоянные каталоги:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/log/proxmox-deployer/
/var/backups/proxmox-configuration/
/var/backups/proxmox-secrets/
```

State:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── last-run.json
├── version
├── last-revision
└── bootstrap-complete
```

PVE Configuration log:

```text
/var/log/proxmox-deployer/configure-pve.log
```

Stable status command:

```text
/usr/local/sbin/pve-configuration-status
```

Главный принцип:

> Public Bootstrap отвечает за доступ к private source of truth, resume и выбор точной revision. PVE Configuration отвечает за воспроизводимое состояние Proxmox host. Credentials не ротируются молча, а один configuration run никогда не должен смешивать две private Git revisions.
