# Public Bootstrap и PVE Configuration — текущее состояние

## Статус

Проект использует два компонента с разными обязанностями:

```text
PUBLIC BOOTSTRAP
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=10

PVE CONFIGURATION
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=20
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

### Общая orchestration lock

Public Bootstrap и прямой PVE Configuration используют один lock:

```text
/run/lock/proxmox-orchestration.lock
```

Bootstrap удерживает lock на всём пути: Git refresh → handoff → PVE Configuration → marker/finalize. PVE Configuration получает открытый fd lock по наследованию. При прямом запуске `configure-pve.sh` берёт тот же lock самостоятельно.

Поэтому одновременно не могут выполняться:

```text
bootstrap + bootstrap
bootstrap + configure-pve.sh
configure-pve.sh + configure-pve.sh
```

Это исключает переключение canonical checkout во время уже выполняющегося configuration run.

### Чистый первый запуск

```text
root/PVE check
→ shared orchestration lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ private repo authorization
→ temporary root-owned shallow checkout main
→ определить точный HEAD
→ PVE_CONFIGURATION_SOURCE_REVISION=<HEAD>
→ запуск PVE Configuration с унаследованным lock
→ создать root-trusted canonical runtime
→ cleanup /var/lib/proxmox-bootstrap
→ bootstrap-complete
```

Если Deploy Key ещё не зарегистрирован, Public Bootstrap показывает public half, ждёт подтверждение пользователя и повторно проверяет доступ. Private keys и token secrets через Git не передаются.

### Resume незавершённого первого запуска

Отсутствие `bootstrap-complete` не означает, что созданный permanent runtime надо удалять.

Если canonical runtime уже готов для Git refresh:

```text
pvedeploy существует
canonical Deploy Key существует
known_hosts существует
SSH config существует
/var/lib/proxmox-deployer/repo/.git существует
```

Public Bootstrap использует существующие credentials и checkout и повторно запускает PVE Configuration. Частично созданные permanent credentials не удаляются и не ротируются автоматически.

Если first-run path всё ещё использует `/var/lib/proxmox-bootstrap/private-repo`, этот checkout считается disposable: после fetch/reset выполняется `git clean -ffdx`, поэтому ignored cache/build artifacts не блокируют строгую source-проверку. Permanent checkout этим правилом не очищается.

### Root trust boundary

Permanent source intentionally разделён с runtime user `pvedeploy`:

```text
/var/lib/proxmox-deployer
→ root:pvedeploy 0750

/var/lib/proxmox-deployer/repo
→ root-owned
→ group/other write запрещён

/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
→ root:root 0600

/etc/proxmox-deployer/ssh/config
→ root:root 0600
```

`pvedeploy` не владеет Git credential и не может менять canonical checkout или `.git`. При этом он может читать project source и использовать собственные deployment credentials/runtime.

Public Bootstrap v10 перед permanent refresh переводит старую `pvedeploy`-owned установку в эту модель. После этого canonical Git operations выполняются от root. Перед handoff ownership и отсутствие group/other write проверяются явно.

### Повторный запуск и local drift

Перед обновлением canonical checkout Public Bootstrap проверяет:

```text
root trust boundary соблюдена
origin == git@github.com:zsergeyru/proxmox.git
Git worktree полностью clean
```

Dirty/staged/untracked/ignored state вызывает STOP. Bootstrap не делает `reset --hard/clean` поверх локального drift и не уничтожает локальные данные молча.

Только для заранее подтверждённого root-trusted clean checkout выполняется:

```text
fetch main от root
→ reset --hard FETCH_HEAD
→ clean -ffd
→ восстановить/проверить root ownership
→ повторная clean-check
→ определить SHA
→ handoff
```

## Одна private revision на один run

Public Bootstrap передаёт:

```text
PVE_CONFIGURATION_SOURCE_REVISION=<40-char SHA>
```

PVE Configuration ещё **до `source lib/*.sh`** проверяет executable source checkout:

```text
процесс = root
source regular files/directories = root-owned
source не writable для group/other
HEAD == expected SHA
tracked/staged/untracked/ignored drift отсутствует
```

Только после этого загружаются модули. Таким образом записанный SHA соответствует реально исполняемому коду, а менее привилегированный runtime user не может подменить код перед root execution.

Canonical checkout после sync обязан иметь тот же SHA и root ownership contract. Если `main` успел измениться между source checkout и canonical fetch/clone, текущий run останавливается вместо смешивания revisions.

`running`, `failed` и `interrupted` state с самого начала содержат source revision. После sync SHA также фиксируется в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

State-файлы обновляются атомарно.

## PVE Configuration

Canonical executable:

```text
scripts/pve/setup/configure-pve.sh
```

Модульная структура:

```text
scripts/pve/setup/
├── configure-pve.sh
├── render-template-cloud-init.py
├── tests/
│   ├── test-template-contract.sh
│   ├── test-template-guest-exec.sh
│   ├── test-token-rollback.sh
│   └── test-source-trust.sh
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

Основная последовательность:

```text
shared lock + root-trusted clean source revision check
→ state=running с source SHA
→ PVE 9 / Debian trixie / KVM preflight
→ configuration snapshot
→ определить состояние template 9000
→ при необходимости восстановить protection совместимого template
→ PVE/Ceph repository policy
→ packages
→ DNS/time/outbound checks
→ active storage + content types
→ Debian 13 LXC appliance
→ pvedeploy contract + config.yaml
→ filesystem trust boundary root/pvedeploy
→ PVE guest SSH identity (pvedeploy)
→ canonical GitHub Deploy Key (root-only)
→ root-owned clean canonical checkout той же source revision
→ managed pool
→ roles/users/API tokens/ACL
→ effective token permission checks
→ token API authentication checks
→ при отсутствии template: source preparation
→ builder VM + provisioning
→ ASCII builder stage telemetry через QGA
→ host-side локализация этапов
→ verification reboot + guest-status check
→ fail-closed guest cleanup
→ seal template
→ final template contract
→ local tooling/status
→ final state
```

PVE Configuration не читает `guest.yaml` или `guests/defaults.yaml` как вход для host configuration.

Canonical GitHub SSH transport использует strict host-key checking, `BatchMode`, `ConnectTimeout` и server-alive limits. HTTP/HTTPS connectivity checks имеют как connection timeout, так и total timeout.

## `pvedeploy` runtime contract

Если Linux user уже существует, PVE Configuration не считает одного имени достаточным. Проверяются:

```text
system UID (<1000)
primary group = pvedeploy
home = /var/lib/pvedeploy
shell = /bin/bash
```

Несовместимый существующий user не меняется автоматически и вызывает STOP.

Filesystem responsibilities:

```text
root
→ canonical repo/.git
→ canonical GitHub Deploy Key и Git SSH config
→ parent /var/lib/proxmox-deployer
→ orchestration state

pvedeploy
→ /var/lib/pvedeploy
→ /var/lib/proxmox-deployer/cache
→ /var/log/proxmox-deployer/audit
→ PVE guest SSH key
→ host-deploy token read access
```

Такой split сохраняет `pvedeploy` пригодным для будущего `deploy-guest`, но исключает возможность заменить source, который позже будет исполнен root.

## LXC readiness

PVE Configuration сначала ищет локальный Debian 13 LXC template, затем пытается обновить catalog:

```text
pveam update успешен
→ newest Debian 13
→ download при необходимости
→ удалить более старые Debian 13 caches

pveam update недоступен + local Debian 13 есть
→ WARNING + использовать local

catalog недоступен + local отсутствует
→ STOP
```

Guest contract хранит family selector:

```text
local:vztmpl/debian-13-standard
```

## Template 9000

Текущий canonical template contract:

```text
VMID: 9000
name: tpl-debian13
Template-Version: 7
ciuser: root
root password: locked
root SSH: public-key only
protection: 1
```

Создание template встроено в PVE Configuration. Отдельного `scripts/pve/create-template.sh` нет.

Host-side stages:

```text
60-template-contract.sh
→ классифицировать VMID 9000
→ проверить полный hardware + Cloud-Init contract

61-template-source.sh
→ capacity/source checks
→ Debian image + SHA512SUMS
→ SHA-512 verification
→ production Cloud-Init renderer

62-template-build.sh
→ builder VM
→ QGA/Cloud-Init/bootstrap
→ ASCII stage-code telemetry
→ локальная русская подпись этапа на PVE host
→ verification reboot
→ normalized guest exec
→ guest-status verification
→ fail-closed cleanup assertions
→ standard Cloud-Init
→ qm template
→ protection=1
```

Production renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Guest-side assets:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

Template v7 добавляет постоянную команду:

```bash
guest-status
```

Она остаётся в `/usr/local/sbin/guest-status` после seal и во всех Full Clone. Вывод ASCII-only и показывает текущий build stage, QGA package/service/enabled state, virtio channel, Cloud-Init, IP и uptime. Такая форма специально пригодна для ранней Proxmox Console до полной настройки locale/font.

Builder публикует в `/var/lib/template-build/bootstrap-status` только ASCII stage identifiers. Кириллица больше не проходит через QGA progress transport; PVE Configuration отображает русские названия уже после чтения кода на host-side. Это устраняет наблюдавшийся mojibake `Ð...`.

Host-visible contract включает:

```text
name/template/version marker
ostype=l26
cpu=host
sockets=1
cores=1
memory=1024
scsihw=virtio-scsi-single
scsi0 на local-lvm
discard=on
iothread=1
ssd=1
system disk >=16G
ide2 = local-lvm:vm-9000-cloudinit,media=cdrom
VirtIO net0 / bridge=vmbr0
boot order from scsi0
agent=1
vga=std
serial0=socket
ciuser=root
ciupgrade=0
ipconfig0=ip=dhcp
cicustom отсутствует
protection=1 после pipeline
```

Exact `vm-9000-cloudinit` check нужен, чтобы обычный ISO/CD-ROM не мог пройти Cloud-Init contract только благодаря `media=cdrom`.

`template_guest_exec` считает incomplete structured QGA result (`pid` без завершения либо `exited=0`), signal и ненулевой exitcode ошибкой. Timeout больше не может быть принят за stdout успешной команды.

Guest cleanup до seal подтверждает отсутствие `debian`, machine-id, SSH host keys, `/root/.ssh`, builder state и builder scripts. Дополнительно подтверждается наличие executable `guest-status`, которая является частью guest contract v7.

Совместимый template без `protection=1` может получить protection после snapshot. Другие contract mismatches вызывают STOP. Unfinished builder и foreign VMID 9000 не удаляются автоматически.

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

Policy ролей и ACL остаётся additive-only. Новый token создаётся с `privsep=1`; существующий `privsep=0` по принятой policy не меняется автоматически.

Одноразовый secret нового token сначала сохраняется во временный файл, затем owner/mode выставляются до atomic rename. До успешного rename новый token отмечен как pending. Parsing/write failure и обычный error/INT/TERM/EXIT пытаются удалить только этот token через `pveum user token delete`; уже существующие tokens не ротируются автоматически.

Effective permissions API tokens проверяются через:

```text
pveum user token permissions <userid> <tokenid>
```

После этого выполняется реальная token+secret авторизация в локальном Proxmox API.

## CI

Private repository checks включают:

```text
repository validator
Python compile
bash -n
ShellCheck
production Cloud-Init render
cloud-init schema
embedded guest-status bash -n / ShellCheck
exact ASCII builder stage-code set
Template contract unit tests
Guest exec result/timeout + stage-label unit tests
API token rollback unit tests
source trust boundary test
malformed guest directory negative test
whitespace check
```

Validator проверяет **все** `guests/*/guest.yaml`; malformed directory больше не пропускается молча.

CI не заменяет реальный clean PVE build + Full Clone smoke-test, который остаётся отдельным integration check по принятой policy. Для v7 smoke-test также проверяет `guest-status`.

## Runtime и status

State:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── last-run.json
├── version
├── last-revision
└── bootstrap-complete
```

Stable status command:

```text
/usr/local/sbin/pve-configuration-status
```

Главный принцип:

> Public Bootstrap отвечает за safe refresh private source of truth, resume, root trust boundary и выбор точной revision. PVE Configuration является единственным host-side orchestrator. Shared lock, ownership checks, clean worktree checks и source SHA гарантируют, что один run не может смешать revisions или исполнять root-код из writable `pvedeploy` checkout.
