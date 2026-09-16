# Инициализация нового Proxmox VE host

## Статус решения

Используются два компонента с разными обязанностями:

```text
Public Bootstrap
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=5

PVE Configuration
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=13
```

`Public Bootstrap` отвечает за получение/обновление private source of truth и handoff. `PVE Configuration` повторяемо приводит сам Proxmox VE host к ожидаемому состоянию проекта.

Термины `Stage 0` и `Stage 1` являются legacy и не используются в новых инструкциях.

## 1. Исходное состояние

Ожидается новый или частично настроенный Proxmox VE host без обязательного наличия:

- private Git checkout;
- AI control plane;
- Proximo;
- project PVE users/tokens/ACL;
- PVE guest SSH identity;
- Debian VM template `9000`;
- подготовленного Debian 13 LXC appliance.

## 2. Public Bootstrap

Каноническая точка входа:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

Штатная команда первого и последующих запусков:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

С полным обновлением системы:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

`--update-system` только дополнительно запрашивает `apt full-upgrade`; обычный запуск его не навязывает.

### Первый запуск

При отсутствии `bootstrap-complete` Public Bootstrap выполняет:

```text
root + Proxmox check
→ exclusive lock
→ minimal Git/SSH toolset
→ DNS/HTTPS GitHub check
→ temporary read-only Deploy Key
→ private repo/main authorization
→ temporary shallow checkout
→ scripts/pve/setup/configure-pve.sh
→ cleanup temporary area
→ bootstrap-complete
```

Public Bootstrap не содержит внутренние PVE roles, pools, guest model, template builder или AI policy.

Временная область:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

После успешной PVE Configuration она удаляется целиком.

Постоянный marker:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

Legacy `stage0-complete` распознаётся для безопасной миграции и после следующего успешного запуска заменяется новым marker contract.

### Первичная GitHub authorization

Если Deploy Key ещё не зарегистрирован, Public Bootstrap показывает public half и предлагает добавить его в:

```text
zsergeyru/proxmox
Settings → Deploy keys
Allow write access = OFF
```

Private key через Git не передаётся.

### Повторный запуск

После первоначального bootstrap новый keypair не создаётся. Используются:

```text
Linux user pvedeploy
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
/var/lib/proxmox-deployer/repo
```

Алгоритм:

```text
проверить permanent runtime
→ проверить origin canonical checkout
→ проверить read-only доступ к zsergeyru/proxmox/main
→ fetch main
→ reset canonical checkout на FETCH_HEAD
→ clean project checkout
→ запустить актуальный scripts/pve/setup/configure-pve.sh
```

Повреждение permanent credential/checkout приводит к STOP и явному recovery, а не к молчаливой ротации.

## 3. PVE Configuration

Canonical executable:

```text
scripts/pve/setup/configure-pve.sh
```

Он автономно конфигурирует host и рассчитан на повторный запуск.

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

PVE Configuration не использует `guest.yaml`/`guests/defaults.yaml` для host configuration decisions.

## 4. Target state

После успешной конфигурации:

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
├── PVE Configuration state
└── health/status tooling
```

Host-side deploy работает независимо от AI control plane.

## 5. Preflight и safety

PVE Configuration проверяет root, PVE 9.x, Debian trixie, KVM, `vmbr0`, `local`, `local-lvm`, обязательные CLI tools, DNS/outbound и VMID 9000.

Если `9000` уже существует, требуются:

```text
name = tpl-debian13
template = 1
ciuser = root
description содержит template-version=6
```

Старый или неизвестный template не удаляется автоматически:

```text
STOP
→ не удалять
→ не менять protected template молча
→ отдельная осознанная пересборка
```

Перед значимыми host-side изменениями создаётся snapshot:

```text
/var/backups/proxmox-bootstrap/YYYYMMDD-HHMMSS/
```

Он содержит применимые host config files и diagnostics, но не заменяет VM backup/disaster recovery.

## 6. Repository policy и packages

Основной PVE channel:

```text
http://download.proxmox.com/debian/pve
trixie
pve-no-subscription
```

Enterprise PVE repo отключается. Ceph меняется только для однозначной стандартной конфигурации; нестандартная/multi-stanza configuration вызывает STOP.

Минимальный runtime:

```text
git openssh-client python3 python3-yaml curl jq ca-certificates
```

Host-admin набор:

```text
mc htop tmux smartmontools lm-sensors
```

Docker, AI runtimes и application services на PVE host не устанавливаются.

## 7. Storage и Debian LXC appliance

Additive-only policy обеспечивает:

```text
local:
  snippets
  vztmpl

local-lvm:
  images
  rootdir
```

PVE Configuration выполняет:

```text
pveam update
→ найти newest debian-13-standard_*_amd64 archive
→ проверить local:vztmpl
→ скачать при отсутствии
→ verify
```

Guest contract хранит family selector:

```text
local:vztmpl/debian-13-standard
```

## 8. Runtime filesystem

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

## 9. PVE guest SSH identity

Один раз создаётся:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Это host-side guest-management identity, отдельная от GitHub и AI keys.

```text
private+public отсутствуют → создать
private существует → не ротировать, восстановить public
private отсутствует, public существует → STOP/recovery
```

## 10. Pool, identities и ACL

Pool:

```text
managed
```

Host-side identity:

```text
deployer@pve!host-deploy
→ человек / host tooling / deploy-guest
→ guest-level /vms
```

AI identity:

```text
ai-agent@pve!infra
→ AI / Proximo
→ /pool/managed
→ template 9000 отдельно как clone source
```

Roles:

```text
AICloneSource
AIManagedGuest
AINetworkUse
AIStorage
AIManagedPool
```

Policy additive-only. Новый token создаётся с `privsep=1`; existing `privsep=0` не меняется автоматически. После настройки проверяются effective permissions и реальная API authentication.

Pool membership и SSH `authorized_keys` — разные границы и автоматически друг друга не меняют.

## 11. Private Git checkout

Canonical checkout:

```text
/var/lib/proxmox-deployer/repo
```

Existing checkout проверяется на ожидаемый origin до fetch/reset. Неожиданный origin → STOP.

GitHub Deploy Key является read-only и отдельным от guest-management SSH key.

## 12. Debian VM template 9000

Builder:

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

Description содержит `template-version=6`.

## 13. State, logs и stable commands

State:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── bootstrap-complete
```

Статусы: `running`, `failed`, `interrupted`, `partial`, `ready`, `ready-with-warnings`.

PVE Configuration log:

```text
/var/log/proxmox-bootstrap/configure-pve.log
```

Терминал может быть цветным, файл лога остаётся plain text.

Canonical status command:

```text
/usr/local/sbin/pve-configuration-status
```

Legacy `/usr/local/sbin/pve-bootstrap-status` временно остаётся wrapper’ом.

## 14. Совместимость старых имён

Старые пути не являются каноническими, но временно работают:

```text
public:  proxmox-bootstrap/init-pve.sh
private: scripts/pve/bootstrap/init-pve.sh
marker:  stage0-complete
```

Они перенаправляют/мигрируют на:

```text
public:  bootstrap-pve.sh
private: scripts/pve/setup/configure-pve.sh
marker:  bootstrap-complete
```

## 15. Главный принцип

> Public Bootstrap получает проверенный private source of truth и запускает PVE Configuration. PVE Configuration повторяемо настраивает host. Credentials, protected template и другие потенциально разрушительные объекты не заменяются молча.
