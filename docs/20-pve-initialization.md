# Инициализация нового Proxmox VE host

## Статус решения

Используются два компонента с разными обязанностями:

```text
Public Bootstrap
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=6

PVE Configuration
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=14
```

`Public Bootstrap` отвечает за получение или обновление private source of truth и передачу управления. `PVE Configuration` повторяемо приводит сам Proxmox VE host к ожидаемому состоянию проекта.

## 1. Исходное состояние

Ожидается новый или частично настроенный Proxmox VE host без обязательного наличия:

- private Git checkout;
- AI control plane;
- Proximo;
- project PVE users/tokens/ACL;
- PVE guest SSH identity;
- Debian VM template `9000`;
- Debian 13 LXC appliance.

Канонический публичный запуск выполняется от `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

Полное системное обновление выполняется только по явному запросу:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

## 2. Public Bootstrap

Канонический public entrypoint:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

### Первый запуск

При отсутствии:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

Public Bootstrap выполняет:

```text
root + Proxmox check
→ exclusive lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ authorization private repo/main
→ temporary shallow checkout
→ запуск PVE Configuration
→ удаление temporary bootstrap runtime
→ запись bootstrap-complete
```

Временная область:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Если Deploy Key ещё не добавлен в GitHub, Public Bootstrap показывает public key и ждёт подтверждение пользователя через терминал. Write access для Deploy Key не включается.

После успешной PVE Configuration временная область полностью удаляется.

Если `bootstrap-complete` отсутствует, но постоянный checkout или постоянный Deploy Key уже существуют, Public Bootstrap останавливается. В новом проекте это считается несогласованным test-state, который нужно очистить перед чистым bootstrap.

### Повторный запуск

При наличии `bootstrap-complete` Public Bootstrap использует постоянный runtime:

```text
pvedeploy
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
→ reset --hard FETCH_HEAD
→ clean -ffd
→ запустить scripts/pve/setup/configure-pve.sh
```

Public Bootstrap не ротирует постоянные credentials и не пересоздаёт damaged runtime автоматически.

## 3. PVE Configuration

Канонический private entrypoint:

```text
scripts/pve/setup/configure-pve.sh
```

Внутренняя структура:

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

`configure-pve.sh` содержит orchestration и вызывает функции модулей. Модули при `source` только объявляют функции и не выполняют самостоятельных действий.

Основная последовательность:

```text
configuration log + exclusive lock
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ configuration snapshot
→ PVE/Ceph repository policy
→ packages
→ DNS/time/outbound checks
→ storage/content types
→ Debian 13 LXC appliance
→ pvedeploy + config.yaml
→ PVE guest SSH identity
→ canonical GitHub Deploy Key
→ canonical private checkout
→ managed pool
→ PVE roles/users/API tokens/ACL
→ effective permission checks
→ token API authentication checks
→ template capacity/source checks
→ template 9000
→ stable local tooling
→ final state/report
```

PVE Configuration не использует `guest.yaml` или `guests/defaults.yaml` как вход для host configuration.

## 4. `--update-system`

Обычная PVE Configuration может выполнять `apt update` и устанавливать необходимые проекту пакеты.

`apt full-upgrade` выполняется только при:

```text
--update-system
```

Это отделяет применение инфраструктурной конфигурации от осознанного полного обновления Proxmox/Debian.

## 5. Preflight и safety

PVE Configuration проверяет:

- запуск от `root`;
- Proxmox VE 9.x;
- Debian trixie;
- `/dev/kvm`;
- `vmbr0`;
- `local` и `local-lvm`;
- обязательные PVE CLI tools;
- конфликт VMID `9000`;
- совместимость существующего template `9000`;
- DNS/outbound access.

Неизвестный VMID `9000`, неожиданный Git origin, потерянный private credential или нестандартная repository configuration не исправляются разрушительно и приводят к STOP.

## 6. Configuration snapshot

Перед значимыми host-side изменениями создаётся snapshot:

```text
/var/backups/proxmox-configuration/YYYYMMDD-HHMMSS/
```

Сохраняются применимые network/hostname/resolver/APT/PVE config files и diagnostics: version, storage, users, roles, ACL, pools, routes и исходный `qm config 9000`.

Это не backup VM и не замена disaster recovery.

## 7. Repository policy и packages

PVE channel без subscription:

```text
http://download.proxmox.com/debian/pve
trixie
pve-no-subscription
```

Enterprise PVE repository отключается.

Ceph configuration меняется консервативно:

- отсутствует → не создавать;
- стандартный no-subscription → оставить;
- стандартный enterprise stanza → переключить на тот же `ceph-*` release без subscription;
- нестандартный/multi-stanza вариант → STOP.

Минимальный runtime:

```text
git
openssh-client
python3
python3-yaml
curl
jq
ca-certificates
```

Host-admin набор:

```text
mc
htop
tmux
smartmontools
lm-sensors
```

Docker и application services на PVE host не устанавливаются.

## 8. Storage и Debian 13 LXC appliance

Базовые storage:

```text
local
local-lvm
```

Additive policy обеспечивает:

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

## 9. Постоянный runtime

Основные каталоги:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/log/proxmox-deployer/
/var/backups/proxmox-configuration/
/var/backups/proxmox-secrets/
```

Canonical private checkout:

```text
/var/lib/proxmox-deployer/repo
```

PVE Configuration log:

```text
/var/log/proxmox-deployer/configure-pve.log
```

Терминал может быть цветным; persistent log хранится без ANSI escape sequences.

## 10. PVE guest SSH identity

Host-side guest-management identity:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Policy:

```text
оба файла отсутствуют
→ создать Ed25519 keypair

private существует
→ не ротировать
→ восстановить public half

private отсутствует, public существует
→ STOP
```

Эта identity не является GitHub Deploy Key и не является AI SSH identity.

## 11. PVE identities и ACL

Host-side / human tooling:

```text
deployer@pve!host-deploy
```

AI / Proximo:

```text
ai-agent@pve!infra
```

`deployer@pve!host-deploy` получает guest-level доступ на `/vms` и необходимые storage/network permissions.

`ai-agent@pve!infra` ограничен рабочей зоной `/pool/managed`; template `9000` доступен отдельно как clone source.

Role/ACL policy additive-only: недостающие разрешения добавляются, существующие дополнительные автоматически не удаляются.

После настройки выполняются effective permission checks и реальная token+secret авторизация через локальный Proxmox API.

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
root SSH: public-key only
protection: 1
```

Description содержит:

```text
template-version=6
```

Если существующий VMID `9000` не соответствует contract, PVE Configuration останавливается и не заменяет protected object автоматически.

## 13. State и status

State directory:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── bootstrap-complete
```

Основная status-команда:

```text
/usr/local/sbin/pve-configuration-status
```

Возможные состояния PVE Configuration:

```text
running
failed
interrupted
partial
ready
ready-with-warnings
```

## 14. Повторный запуск

Штатный повторный запуск начинается с Public Bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

Public Bootstrap сначала обновляет private source of truth, затем запускает текущую PVE Configuration. Внутри PVE Configuration используется отдельный `flock`, поэтому параллельные configuration runs запрещены.

Главный принцип:

> Public Bootstrap отвечает за доступ к private source of truth и handoff. PVE Configuration отвечает за воспроизводимое состояние Proxmox host. Потенциально разрушительные изменения credentials, protected template или неизвестной конфигурации не выполняются молча.
