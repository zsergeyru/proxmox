# Public Bootstrap и PVE Configuration — текущее состояние

## Статус

Инициализация PVE разделена не на «этапы», а на два компонента с разными обязанностями:

```text
PUBLIC BOOTSTRAP
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=5

PVE CONFIGURATION
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=13
```

Термины `Stage 0` и `Stage 1` считаются legacy и используются только там, где нужно объяснить совместимость со старым runtime.

Канонические документы:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
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
→ shallow checkout main
→ запуск PVE Configuration
→ cleanup /var/lib/proxmox-bootstrap
→ bootstrap-complete
```

Если Deploy Key ещё не зарегистрирован, Public Bootstrap показывает public half, ждёт подтверждение пользователя и повторно проверяет доступ. Private keys и token secrets через Git не передаются.

Постоянный marker:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

Legacy marker `/var/lib/proxmox-deployer/state/stage0-complete` распознаётся для миграции. После успешного повторного запуска создаётся новый `bootstrap-complete`.

### Повторный запуск

После завершённого первоначального bootstrap новый Deploy Key не создаётся. Public Bootstrap выполняет безопасный refresh/handoff:

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

Если canonical credential, SSH runtime или checkout отсутствует/повреждён, Public Bootstrap останавливается. Credential автоматически не ротируется.

## PVE Configuration v13

PVE Configuration — каноническая повторяемая конфигурация самого Proxmox host:

```text
scripts/pve/setup/configure-pve.sh
```

Она не читает `guest.yaml` или `guests/defaults.yaml` для host configuration decisions.

Основная последовательность:

```text
exclusive configuration lock
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ existing template 9000 compatibility check
→ host configuration snapshot
→ PVE/Ceph repository policy
→ packages
→ DNS/time/outbound checks
→ storage/content types
→ актуальный Debian 13 LXC appliance
→ pvedeploy + config.yaml
→ PVE guest SSH identity
→ canonical GitHub Deploy Key
→ canonical private checkout
→ managed pool
→ roles/users/API tokens/ACL
→ effective permission checks
→ token API authentication checks
→ template capacity/source checks
→ template 9000, если отсутствует
→ local tooling/status
→ final state
```

### Модульная структура

Монолитный `init-pve-core.sh` удалён. Каноническая структура:

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

Модули только объявляют функции. Порядок реального выполнения явно задан в `configure-pve.sh`.

## PVE guest SSH identity

PVE Configuration создаёт и сохраняет:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Это host-side management credential для `deploy-guest`, отдельный от GitHub и AI identities.

```text
private + public отсутствуют
→ создать Ed25519 keypair

private существует
→ не ротировать
→ восстановить .pub из private

private отсутствует, public существует
→ STOP
→ recovery/explicit rotation
```

Private key не выводится в logs и должен входить во внешнюю backup policy infrastructure secrets.

## LXC readiness

PVE Configuration обеспечивает `local:vztmpl` и выполняет:

```text
pveam update
→ найти наиболее новый debian-13-standard_*_amd64 archive
→ скачать его в local, если отсутствует
→ verify
```

Guest contract хранит selector семейства:

```text
local:vztmpl/debian-13-standard
```

## Template 9000 — root-only v6

Canonical template:

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

Template description содержит `template-version=6`.

Если существующий VMID 9000 не соответствует `tpl-debian13`, `template=1`, `ciuser=root` и Template-Version 6, PVE Configuration останавливается. Protected template автоматически не удаляется и не заменяется.

## Root-only guest management

Template v6 и guest contract используют единый management user:

```text
root:22
```

Root password locked; password/KbdInteractive authentication выключены. Initial management key передаётся при создании guest штатным для VM/LXC способом.

## PVE identities и ACL

PVE Configuration поддерживает две разные PVE identities:

```text
deployer@pve!host-deploy
→ host-side/human tooling
→ guest-level доступ на /vms

ai-agent@pve!infra
→ AI/Proximo
→ guest-level write-zone /pool/managed
→ template 9000 отдельно только как clone source
```

Policy ролей и ACL additive-only: недостающие privileges/ACL добавляются, существующие дополнительные автоматически не удаляются. Новый token создаётся с `privsep=1`; существующий `privsep=0` не меняется молча.

После настройки проверяются effective permissions и реальная token+secret авторизация в локальном Proxmox API.

## `managed` и SSH — разные границы

```text
managed
→ PVE permissions AI

AI public key в guest
→ direct SSH AI
```

Перемещение guest в/из pool не меняет `authorized_keys` автоматически.

## Filesystem/runtime

Постоянные каталоги:

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
└── bootstrap-complete
```

`state.json` использует `component: pve-configuration` и `configuration_version: 13`.

Canonical log PVE Configuration:

```text
/var/log/proxmox-bootstrap/configure-pve.log
```

Терминальный вывод цветной; log сохраняется без ANSI escape-кодов.

## Stable commands

Canonical status command:

```text
/usr/local/sbin/pve-configuration-status
```

Legacy `/usr/local/sbin/pve-bootstrap-status` временно сохраняется как wrapper с предупреждением.

Планируемый deployer:

```text
/usr/local/sbin/deploy-guest
→ /var/lib/proxmox-deployer/repo/scripts/pve/deploy-guest.py
```

Пока source отсутствует, состояние корректно остаётся `partial`.

## Совместимость старых путей

Старый private path временно сохранён:

```text
scripts/pve/bootstrap/init-pve.sh
```

Он только предупреждает об устаревшем имени и передаёт управление:

```text
scripts/pve/setup/configure-pve.sh
```

Старый public `proxmox-bootstrap/init-pve.sh` также является compatibility loader для нового `bootstrap-pve.sh`.

## Safety на повторном запуске

Автоматически исправляются только однозначные безопасные вещи. STOP требуют:

- неизвестный/несовместимый VMID 9000;
- disabled project PVE user;
- token существует, а local secret потерян;
- guest private SSH key потерян при сохранившемся public;
- неожиданный Git origin;
- нестандартная PVE/Ceph repository configuration;
- повреждённый permanent Public Bootstrap runtime.

Главный принцип:

> Public Bootstrap отвечает только за получение проверенного private source of truth и handoff. PVE Configuration воспроизводимо приводит host к нужному состоянию. Credentials, protected template и другие потенциально разрушительные объекты не заменяются автоматически.
