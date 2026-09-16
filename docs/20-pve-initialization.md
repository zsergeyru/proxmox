# Инициализация нового Proxmox VE host

## Статус решения

Используются два компонента с разными обязанностями:

```text
Public Bootstrap
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=7

PVE Configuration
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=16
```

`Public Bootstrap` отвечает за получение или обновление private source of truth, выбор точной Git revision и передачу управления. `PVE Configuration` повторяемо приводит сам Proxmox VE host к ожидаемому состоянию проекта, включая создание базового Debian template `9000`.

## 1. Канонический запуск

Ожидается новый или частично настроенный Proxmox VE host. Основная команда выполняется от `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

Полное системное обновление выполняется только по явному запросу:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

Одна и та же основная команда используется для:

- первого запуска;
- штатного повторного применения конфигурации;
- продолжения незавершённого первого запуска после исправления причины ошибки.

## 2. Public Bootstrap

Канонический public entrypoint:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

### Чистый первый запуск

При отсутствии permanent runtime выполняется:

```text
root + Proxmox check
→ exclusive lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ authorization private repo/main
→ temporary shallow checkout
→ определить точный private HEAD
→ запустить PVE Configuration с PVE_CONFIGURATION_SOURCE_REVISION=<HEAD>
→ удалить temporary bootstrap runtime
→ записать bootstrap-complete
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

Если Deploy Key ещё не добавлен в GitHub, Public Bootstrap показывает public key и ждёт подтверждение пользователя через терминал. Write access не включается.

### Resume после частичной ошибки

Если PVE Configuration остановилась после создания permanent runtime, следующий запуск **не требует очистки** `/etc/proxmox-deployer` или `/var/lib/proxmox-deployer`.

Если уже готовы:

```text
pvedeploy
canonical Deploy Key
canonical known_hosts + SSH config
/var/lib/proxmox-deployer/repo/.git
```

Public Bootstrap считает это resumable permanent runtime, обновляет canonical checkout и повторно запускает PVE Configuration.

Если permanent runtime создан только частично, Public Bootstrap продолжает first-run path через temporary runtime. Существующие постоянные credentials не удаляются и не ротируются автоматически.

Такой recovery принципиален для API tokens: secret выдаётся Proxmox только в момент создания токена, поэтому удаление локального token secret вместо resume недопустимо.

### Повторный запуск после завершённого bootstrap

При наличии:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

Public Bootstrap выполняет:

```text
проверить permanent runtime
→ проверить origin canonical checkout
→ проверить read-only доступ к zsergeyru/proxmox/main
→ fetch main
→ reset --hard FETCH_HEAD
→ clean -ffd
→ определить точный HEAD
→ запустить PVE Configuration с этим SHA
→ обновить bootstrap-complete
```

Повреждённые permanent credentials не ротируются автоматически.

## 3. Одна Git revision на один запуск

После того как Public Bootstrap выбрал private revision, эта revision является immutable input текущего configuration run.

```text
PVE_CONFIGURATION_SOURCE_REVISION=<40-char SHA>
```

PVE Configuration должна привести canonical checkout ровно к этому SHA. Она не должна во время уже выполняющегося run молча переключаться на новый `main`.

Если `main` изменился между temporary checkout и canonical clone/fetch, выполнение останавливается. Повторный Public Bootstrap начинает новый run уже из новой согласованной revision.

Фактически применённый SHA сохраняется в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

## 4. PVE Configuration

Канонический private entrypoint:

```text
scripts/pve/setup/configure-pve.sh
```

Структура:

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
    ├── 60-template-contract.sh
    ├── 61-template-source.sh
    ├── 62-template-build.sh
    └── 70-tooling.sh
```

Модули при `source` только объявляют функции. Основная последовательность:

```text
configuration log + exclusive lock
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ configuration snapshot
→ template state/contract
→ при необходимости восстановить protection совместимого template
→ repository policy
→ packages
→ DNS/time/outbound checks
→ storage/content types
→ Debian 13 LXC appliance
→ pvedeploy + config.yaml
→ PVE guest SSH identity
→ canonical GitHub Deploy Key
→ canonical private checkout той же source revision
→ managed pool
→ roles/users/API tokens/ACL
→ effective token permission checks
→ token API authentication checks
→ если 9000 отсутствует: source/image/Cloud-Init preparation
→ builder VM provisioning
→ verification reboot
→ guest cleanup
→ seal template
→ final template contract
→ local tooling/status
→ final state
```

PVE Configuration не использует `guest.yaml` или `guests/defaults.yaml` как вход для host configuration.

## 5. `--update-system`

Обычный run может выполнить `apt update` и установить необходимые project packages.

Только явный:

```bash
--update-system
```

включает:

```text
apt full-upgrade
```

## 6. Preflight и safety

Host preflight проверяет:

- запуск от `root`;
- Proxmox VE 9.x;
- Debian trixie;
- `/dev/kvm`;
- `vmbr0`;
- `local` и `local-lvm`;
- обязательные PVE CLI tools;
- DNS/outbound access.

Состояние VMID `9000` проверяется отдельным template-contract stage сразу после configuration snapshot и до остальных host-side изменений. Неизвестный VMID `9000`, неожиданный Git origin, потерянный private credential или нестандартная repository configuration не исправляются разрушительно и приводят к STOP.

## 7. Configuration snapshot

Перед значимыми host-side изменениями создаётся snapshot:

```text
/var/backups/proxmox-configuration/YYYYMMDD-HHMMSS/
```

Сохраняются применимые network/hostname/resolver/APT/PVE config files и diagnostics: version, storage, users, roles, ACL, pools, routes и исходный `qm config 9000`.

Это не backup VM и не замена disaster recovery.

## 8. Repository policy и packages

PVE channel без subscription:

```text
http://download.proxmox.com/debian/pve
trixie
pve-no-subscription
```

Enterprise PVE repository отключается. Ceph configuration меняется консервативно: стандартный enterprise stanza переводится на тот же `ceph-*` release без subscription; нестандартный/multi-stanza вариант вызывает STOP.

Минимальный runtime:

```text
git openssh-client python3 python3-yaml curl jq ca-certificates
```

Host-admin набор:

```text
mc htop tmux smartmontools lm-sensors
```

Docker и application services на PVE host не устанавливаются.

## 9. Storage и Debian 13 LXC appliance

Additive policy обеспечивает:

```text
local:
  snippets
  vztmpl

local-lvm:
  images
  rootdir
```

PVE Configuration выполняет `pveam update`, находит newest `debian-13-standard_*_amd64` и скачивает template при отсутствии.

Guest contract хранит family selector:

```text
local:vztmpl/debian-13-standard
```

## 10. Постоянный runtime

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

Template build использует тот же lock, state и log; отдельного host-side template orchestrator нет.

## 11. PVE guest SSH identity

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Policy:

```text
оба отсутствуют → создать Ed25519 keypair
private существует → не ротировать, восстановить public half
private отсутствует, public существует → STOP
```

Эта identity не является GitHub Deploy Key и не является AI SSH identity.

## 12. PVE identities и ACL

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

Role/ACL policy остаётся additive-only. После настройки effective permissions API tokens проверяются штатной командой Proxmox:

```text
pveum user token permissions <userid> <tokenid>
```

Затем выполняется реальная token+secret авторизация через локальный Proxmox API.

## 13. Debian VM template 9000

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

Отдельного `scripts/pve/create-template.sh` нет. Host-side pipeline встроен в PVE Configuration:

```text
60-template-contract.sh
→ состояние/contract VMID 9000

61-template-source.sh
→ capacity/source
→ Debian image + SHA512SUMS
→ SHA-512 verification
→ temporary Cloud-Init из versioned assets

62-template-build.sh
→ builder VM
→ provisioning/QGA/Cloud-Init
→ verification reboot
→ kernel/framebuffer/console checks
→ finalize guest
→ standard Cloud-Init
→ qm template
→ protection=1
```

Guest-side assets:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

Существующий template проверяется по полному host-visible contract:

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
protection=1 после pipeline
```

Отсутствие защиты у в остальном совместимого template исправляется автоматически после snapshot.

Если VMID `9000` содержит незавершённый `builder-debian13`, PVE Configuration останавливается с диагностическим сообщением и **не удаляет** VM/disks/snippet автоматически. Посторонняя VM или несовместимый template также вызывают STOP без destructive overwrite.

## 14. State и status

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── bootstrap-complete
```

Status-команда:

```text
/usr/local/sbin/pve-configuration-status
```

Возможные состояния:

```text
running
failed
interrupted
partial
ready
ready-with-warnings
```

Главный принцип:

> Public Bootstrap отвечает за доступ к private source of truth, resume и выбор точной revision. PVE Configuration является единственным host-side orchestrator и отвечает за воспроизводимое состояние Proxmox host, включая template 9000. Потенциально разрушительные изменения credentials и template state не выполняются молча.
