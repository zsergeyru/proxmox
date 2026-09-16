# Инициализация нового Proxmox VE host

## Статус решения

Используются два компонента с разными обязанностями:

```text
Public Bootstrap
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=7

PVE Configuration
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=15
```

`Public Bootstrap` отвечает за получение или обновление private source of truth, выбор точной Git revision и передачу управления. `PVE Configuration` повторяемо приводит сам Proxmox VE host к ожидаемому состоянию проекта.

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
    └── 70-template-tooling.sh
```

Модули при `source` только объявляют функции. Основная последовательность:

```text
configuration log + exclusive lock
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ configuration snapshot
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
→ template capacity/source checks
→ template 9000
→ local tooling/status
→ final state
```

PVE Configuration не использует `guest.yaml` или `guests/defaults.yaml` как вход для host configuration.

## 5. `--update-system`

Обычный run может выполнить `apt update` и установить необходимые project packages.

Только явный:

```text
--update-system
```

включает:

```text
apt full-upgrade
```

## 6. Preflight и safety

Проверяются:

- запуск от `root`;
- Proxmox VE 9.x;
- Debian trixie;
- `/dev/kvm`;
- `vmbr0`;
- `local` и `local-lvm`;
- обязательные PVE CLI tools;
- конфликт VMID `9000`;
- полный host-visible contract существующего template `9000`;
- DNS/outbound access.

Неизвестный VMID `9000`, неожиданный Git origin, потерянный private credential или нестандартная repository configuration не исправляются разрушительно и приводят к STOP.

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
```

`protection=1` также обязателен после `ensure_template`; отсутствие защиты у в остальном совместимого template допускается только до snapshot и исправляется автоматически после него.

Любое другое несовпадение вызывает STOP и не приводит к автоматической замене VMID 9000.

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

> Public Bootstrap отвечает за доступ к private source of truth, resume и выбор точной revision. PVE Configuration отвечает за воспроизводимое состояние Proxmox host и не смешивает разные revisions внутри одного запуска. Потенциально разрушительные изменения credentials и protected template не выполняются молча.
