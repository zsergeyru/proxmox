# Bootstrap — текущее состояние

## Статус

Bootstrap PVE разделён на две исполняемые стадии:

```text
PUBLIC Stage 0
zsergeyru/proxmox-bootstrap/init-pve.sh
STAGE0_VERSION=4

PRIVATE Stage 1
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
BOOTSTRAP_VERSION=12
```

Канонические документы:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`25-pve-access-control.md`](25-pve-access-control.md);
- [`30-guest-manifest.md`](30-guest-manifest.md);
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## Public Stage 0

При первичной установке Stage 0 остаётся минимальным zero-day loader:

```text
root/PVE check
→ exclusive lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ private repo access
→ shallow checkout main
→ запуск private Stage 1
→ cleanup /var/lib/proxmox-bootstrap
→ stage0-complete
```

Stage 0 не знает про pools, PVE roles, API identities, VMID plan, template implementation или guest manifests.

Если Deploy Key ещё не зарегистрирован, public loader показывает public half, ждёт подтверждение пользователя и повторяет проверку. Private keys и token secrets через Git не передаются.

После успешного handoff временный `/var/lib/proxmox-bootstrap` удаляется целиком. Постоянный marker:

```text
/var/lib/proxmox-deployer/state/stage0-complete
```

### Повторный public запуск

Начиная со `STAGE0_VERSION=4`, та же public curl-команда является штатной точкой входа и после уже завершённой Stage 0.

При наличии `stage0-complete` public bootstrap **не создаёт новый Deploy Key и не повторяет zero-day setup**. Он выполняет только безопасный refresh/handoff:

```text
проверить pvedeploy
→ проверить canonical Deploy Key/known_hosts/SSH config
→ проверить canonical checkout /var/lib/proxmox-deployer/repo
→ проверить ожидаемый origin
→ подтвердить read-only доступ к zsergeyru/proxmox/main
→ fetch main
→ reset --hard FETCH_HEAD
→ clean -ffd
→ запустить уже обновлённую private Stage 1
```

Обновление выполняется от имени `pvedeploy` и использует постоянный SSH config private Stage 1. Если canonical credential, SSH runtime или checkout отсутствует/повреждён, public bootstrap останавливается: новый credential автоматически не генерируется.

Обычный повторный запуск:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/init-pve.sh | bash
```

Повторный запуск с полным системным обновлением:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/init-pve.sh | bash -s -- --update-system
```

## Private Stage 1 v12

Stage 1 автономно настраивает PVE host. Он не использует `guest.yaml` или `guests/defaults.yaml` как вход для host bootstrap.

Основная последовательность:

```text
exclusive run lock
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ проверка canonical template 9000, если он уже существует
→ configuration snapshot
→ PVE/Ceph repository policy
→ packages
→ DNS/time/outbound checks
→ active storage + content types
→ актуальный Debian 13 LXC appliance
→ pvedeploy + config.yaml
→ PVE guest SSH identity
→ canonical GitHub Deploy Key
→ canonical private checkout
→ managed pool
→ roles/users/API tokens/ACL
→ effective permission checks
→ token API authentication checks
→ capacity/source checks
→ template 9000, если отсутствует
→ local wrappers/status
→ final state
```

## PVE guest SSH identity

Stage 1 создаёт и сохраняет:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Это host-side management credential для `deploy-guest`, отдельный от GitHub и AI identities.

Поведение:

```text
private + public отсутствуют
→ создать Ed25519 keypair

private существует
→ не ротировать
→ восстановить .pub из private

private отсутствует, public существует
→ STOP
→ требуется recovery/осознанная rotation
```

Private key не выводится в logs и должен входить во внешнюю backup policy infrastructure secrets.

## LXC readiness

Stage 1 обеспечивает `local:vztmpl` и выполняет:

```text
pveam update
→ найти наиболее новый debian-13-standard_*_amd64 archive
→ скачать его в local, если отсутствует
→ verify
```

Точное имя archive не хардкодится в `guests/defaults.yaml`. Guest contract хранит selector семейства:

```text
local:vztmpl/debian-13-standard
```

Будущий deployer разрешает selector в установленный runtime archive.

## Template 9000 — root-only v6

Текущий canonical template:

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

Template description содержит машинно-читаемый marker:

```text
template-version=6
```

### Проверка существующего template

Stage 1 v12 не считает любой `tpl-debian13` автоматически совместимым. Для существующего VMID 9000 проверяются как минимум:

```text
name = tpl-debian13
template = 1
ciuser = root
description содержит template-version=6
```

Если найден старый Template v4 (`ops`) или другой несовместимый 9000:

```text
STOP
→ ничего не удалять
→ ничего не пересобирать автоматически
→ потребовать отдельную осознанную замену protected template
```

Это намеренная safety policy.

Если 9000 отсутствует, Stage 1 после capacity/source checks запускает актуальный builder и затем проверяет `template=1`, `protection=1`, `ciuser=root` и marker версии.

## Root-only guest management

Template v6 и новый guest contract используют единый management user:

```text
root:22
```

Root password locked. Password/KbdInteractive authentication выключены.

Initial guest access не является bootstrap capability:

```text
VM  → Cloud-Init public keys для root
LXC → ssh-public-keys для root
→ start
→ verify SSH
→ только затем optional base/git/docker/...
```

## PVE identities и ACL

Stage 1 поддерживает две разные PVE identities:

```text
deployer@pve!host-deploy
→ host-side/human tooling
→ guest-level доступ на /vms

ai-agent@pve!infra
→ AI/Proximo
→ guest-level write-zone /pool/managed
→ template 9000 отдельно только как clone source
```

Разрешённые storage/network paths выдаются отдельно согласно [`25-pve-access-control.md`](25-pve-access-control.md).

Policy ролей и ACL additive-only: недостающие privileges/ACL добавляются, существующие дополнительные автоматически не удаляются.

Новый token создаётся с `privsep=1`. Существующий `privsep=0` не меняется молча; выводится warning.

После настройки выполняются:

```text
pveum effective permissions
+
реальная token+secret авторизация в локальном Proxmox API
```

## `managed` и SSH — разные границы

Stage 1 создаёт pool `managed` и PVE ACL, но не управляет AI public key внутри каждого guest.

```text
managed
→ PVE permissions AI

AI public key в guest
→ direct SSH AI
```

Перемещение guest в/из pool не должно автоматически менять `authorized_keys`.

## Filesystem/runtime

Постоянные файлы Stage 1:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/log/proxmox-deployer/
/var/log/proxmox-bootstrap/
/var/backups/proxmox-bootstrap/
/var/backups/proxmox-secrets/
```

State:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── last-run.json
├── version
├── last-revision
└── stage0-complete
```

`state.json` v12 отражает также ожидаемую `template_version: 6`.

## Canonical private checkout

```text
/var/lib/proxmox-deployer/repo
```

Если checkout уже существует, origin проверяется до fetch/reset. Неожиданный origin не переписывается автоматически.

Canonical GitHub Deploy Key и `pve_guest_ed25519` никогда не переиспользуются друг вместо друга.

## `deploy-guest`

Планируемый source:

```text
scripts/pve/deploy-guest.py
```

Stable wrapper после реализации:

```text
/usr/local/sbin/deploy-guest
```

Пока source отсутствует, bootstrap корректно сообщает `partial`; это не должно маскироваться как `ready`.

## Повторный запуск

Public entrypoint сначала обновляет canonical private checkout и запускает свежую Stage 1. Сама Stage 1 рассчитана на повторный запуск и использует `flock`. Однако safety-sensitive drift не исправляется молча:

- неизвестный/несовместимый VMID 9000 → STOP;
- disabled project PVE user → STOP;
- token существует, а secret потерян → STOP;
- guest private SSH key потерян при сохранившемся public → STOP;
- неожиданный Git origin → STOP;
- нестандартная PVE/Ceph repo configuration → STOP.

Главный принцип:

> Public Stage 0 создаёт zero-day credential только один раз; после завершения Stage 0 тот же public entrypoint лишь обновляет проверенный private source of truth и передаёт управление свежей Stage 1. Замена credentials, protected template и другие потенциально разрушительные миграции требуют отдельного осознанного действия.
