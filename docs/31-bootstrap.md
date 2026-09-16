# Public Bootstrap и PVE Configuration — текущее состояние

## Статус

Проект использует два компонента с разными обязанностями:

```text
PUBLIC BOOTSTRAP
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=6

PVE CONFIGURATION
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=14
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

### Первый запуск

```text
root/PVE check
→ exclusive lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ private repo authorization
→ temporary shallow checkout main
→ запуск PVE Configuration
→ cleanup /var/lib/proxmox-bootstrap
→ bootstrap-complete
```

Если Deploy Key ещё не зарегистрирован, Public Bootstrap показывает public half, ждёт подтверждение пользователя и повторно проверяет доступ. Private keys и token secrets через Git не передаются.

Постоянный marker:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

Если marker отсутствует, но permanent checkout или permanent Deploy Key уже существуют, запуск останавливается как на несогласованном test-state. Для нового проекта такой state не мигрируется автоматически.

### Повторный запуск

После завершённого первоначального bootstrap новый Deploy Key не создаётся. Public Bootstrap выполняет refresh/handoff:

```text
проверить pvedeploy
→ проверить canonical Deploy Key/known_hosts/SSH config
→ проверить /var/lib/proxmox-deployer/repo и origin
→ подтвердить read-only доступ к zsergeyru/proxmox/main
→ fetch main
→ reset --hard FETCH_HEAD
→ clean -ffd
→ запустить актуальный scripts/pve/setup/configure-pve.sh
```

Git-команды выполняются от имени `pvedeploy` с постоянным SSH config.

Повреждённый permanent runtime не восстанавливается путём молчаливой ротации credentials.

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
→ canonical private checkout
→ managed pool
→ roles/users/API tokens/ACL
→ effective permission checks
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

включает:

```text
apt full-upgrade
```

## PVE guest SSH identity

PVE Configuration создаёт и сохраняет:

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
```

Private key должен входить во внешнюю backup policy infrastructure secrets.

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

Template description содержит:

```text
template-version=6
```

Если существующий VMID `9000` не соответствует contract, PVE Configuration выполняет STOP. Protected template автоматически не удаляется и не заменяется.

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

Используются две разные PVE identities:

```text
deployer@pve!host-deploy
→ человек / host-side tooling / deploy-guest
→ guest-level доступ на /vms

ai-agent@pve!infra
→ AI / Proximo
→ write-zone /pool/managed
→ template 9000 отдельно как clone source
```

Разрешённые storage/network paths выдаются отдельно согласно [`25-pve-access-control.md`](25-pve-access-control.md).

Policy ролей и ACL additive-only: недостающие privileges/ACL добавляются, существующие дополнительные автоматически не удаляются.

Новый token создаётся с `privsep=1`. После настройки выполняются effective permission checks и реальная token+secret авторизация в локальном Proxmox API.

## `managed` и SSH — разные границы

```text
managed pool
→ PVE permissions AI

AI public key в guest
→ direct SSH AI
```

Pool membership не управляет `authorized_keys` внутри guest OS.

## Runtime

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

## Canonical private checkout

```text
/var/lib/proxmox-deployer/repo
```

Existing checkout проверяется на ожидаемый origin до fetch/reset. Неожиданный origin не переписывается автоматически.

Canonical GitHub Deploy Key и `pve_guest_ed25519` никогда не переиспользуются друг вместо друга.

## Stable status command

```text
/usr/local/sbin/pve-configuration-status
```

Команда читает:

```text
/var/lib/proxmox-deployer/state/state.json
```

## `deploy-guest`

Планируемый source:

```text
scripts/pve/deploy-guest.py
```

Stable wrapper после реализации:

```text
/usr/local/sbin/deploy-guest
```

Пока source отсутствует, PVE Configuration сообщает `partial`.

## Повторный запуск и safety

Штатный повторный запуск всегда начинается с Public Bootstrap, который обновляет canonical private checkout и запускает актуальную PVE Configuration.

Safety-sensitive drift не исправляется молча:

- неизвестный/несовместимый VMID `9000` → STOP;
- disabled project PVE user → STOP;
- token существует, а secret потерян → STOP;
- guest private SSH key потерян при сохранившемся public → STOP;
- неожиданный Git origin → STOP;
- нестандартная PVE/Ceph repository configuration → STOP.

Главный принцип:

> Public Bootstrap отвечает только за private source acquisition/refresh и handoff. PVE Configuration отвечает за воспроизводимое состояние PVE host. Credentials, protected template и другие потенциально разрушительные объекты не меняются автоматически при неоднозначном state.
