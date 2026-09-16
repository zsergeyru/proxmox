# Инициализация нового Proxmox VE host

## Статус решения

Используются два компонента с разными обязанностями:

```text
Public Bootstrap
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=10

PVE Configuration
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=19
```

`Public Bootstrap` отвечает за безопасное получение/обновление private source of truth, выбор точной Git revision, root trust boundary canonical source и handoff. `PVE Configuration` является единственным host-side orchestrator и повторяемо приводит Proxmox VE к ожидаемому состоянию проекта, включая template `9000`.

## 1. Канонический запуск

Обычный запуск от `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

Полное системное обновление только по явному запросу:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

Одна команда используется для first run, штатного rerun и resume после частичной ошибки.

## 2. Общая orchestration lock

Public Bootstrap и PVE Configuration используют одну блокировку:

```text
/run/lock/proxmox-orchestration.lock
```

Bootstrap берёт её до любых действий с private checkout и удерживает до завершения PVE Configuration и финализации marker. Child `configure-pve.sh` получает fd 9 по наследованию и проверяет его. При прямом запуске PVE Configuration берёт этот же lock самостоятельно.

Таким образом нельзя одновременно выполнять два bootstrap/configuration run и нельзя переключить canonical repo во время уже работающей PVE Configuration.

## 3. Первый запуск и resume

Чистый первый запуск:

```text
root + Proxmox check
→ shared lock
→ minimal Git/SSH packages
→ DNS/HTTPS GitHub check
→ temporary read-only GitHub Deploy Key
→ authorization private repo/main
→ temporary root-owned shallow checkout
→ определить exact HEAD
→ PVE_CONFIGURATION_SOURCE_REVISION=<HEAD>
→ PVE Configuration
→ permanent root-trusted runtime
→ удалить temporary runtime
→ bootstrap-complete
```

Temporary area:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Если первый запуск остановился после создания permanent runtime, повторный запуск не требует удаления `/etc/proxmox-deployer` или `/var/lib/proxmox-deployer`. Существующие credentials сохраняются; API token secret и SSH private keys не ротируются автоматически.

Temporary checkout считается disposable. При его повторном использовании Public Bootstrap выполняет fresh fetch/reset и `git clean -ffdx`, поэтому ignored cache/build artifacts не могут заблокировать строгую source-проверку. Это правило не распространяется на permanent checkout, для которого local drift по-прежнему вызывает STOP.

## 4. Root trust boundary и permanent refresh

Canonical source, из которого `root` запускает PVE Configuration, не является рабочим каталогом `pvedeploy`.

Ожидаемая модель:

```text
/var/lib/proxmox-deployer
→ root:pvedeploy 0750

/var/lib/proxmox-deployer/repo
→ root-owned tree
→ group/other write запрещён

/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
→ root:root 0600

/etc/proxmox-deployer/ssh/config
→ root:root 0600
```

Это важно не только для файлов внутри `repo`, но и для его родителя: если `/var/lib/proxmox-deployer` был бы writable для `pvedeploy`, ограниченный user мог бы удалить/переименовать root-owned `repo` и подменить каталог целиком.

Public Bootstrap v10 автоматически переводит старую `pvedeploy`-owned модель в root trust boundary до запуска новой PVE Configuration.

Для rerun Public Bootstrap проверяет:

```text
root trust boundary canonical source
origin = git@github.com:zsergeyru/proxmox.git
tracked drift отсутствует
staged drift отсутствует
untracked drift отсутствует
ignored drift отсутствует
```

Если обнаружено локальное изменение, Bootstrap делает STOP. `reset --hard` и `clean` не используются как способ молча стереть локальный drift.

Только для root-trusted clean checkout разрешено:

```text
fetch main от root
→ reset --hard FETCH_HEAD
→ clean -ffd
→ восстановить/проверить root ownership и non-writable boundary
→ повторная clean-check
→ определить SHA
→ handoff
```

`pvedeploy` может читать source, необходимый будущему `deploy-guest`, но не может изменять canonical Git checkout, `.git` metadata или GitHub Deploy Key.

## 5. Одна Git revision на один run

Public Bootstrap передаёт:

```text
PVE_CONFIGURATION_SOURCE_REVISION=<40-char SHA>
```

До загрузки `lib/*.sh` PVE Configuration проверяет сам source checkout:

```text
запуск от root
Git root ожидаемый
source tree root-owned
regular files/directories не writable для group/other
HEAD == source revision
worktree полностью clean, включая ignored files
```

Только после этого source-модули становятся исполняемым кодом текущего run. Поэтому `root` не исполняет PVE Configuration из checkout, контролируемого `pvedeploy`.

Canonical private checkout после sync обязан иметь тот же SHA и тот же root trust contract. Если `main` изменился между временным checkout и canonical fetch/clone, run останавливается вместо смешивания revisions.

Source revision фиксируется в state уже при `status=running`; ранний failure/interruption тоже сохраняет правильный SHA. После canonical sync SHA записывается в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

## 6. PVE Configuration

Структура:

```text
scripts/pve/setup/
├── configure-pve.sh
├── render-template-cloud-init.py
├── tests/
│   ├── test-template-contract.sh
│   ├── test-template-guest-exec.sh
│   └── test-token-rollback.sh
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
shared lock + root-trusted source SHA/clean check
→ state=running
→ PVE 9 / Debian trixie / KVM preflight
→ configuration snapshot
→ template 9000 state/contract
→ optional protection repair
→ repository policy
→ packages
→ DNS/time/outbound checks
→ storage/content types
→ Debian 13 LXC appliance
→ pvedeploy runtime contract
→ root/pvedeploy filesystem boundary
→ PVE guest SSH identity
→ root-only canonical GitHub identity
→ root-owned canonical checkout exact source revision
→ managed pool
→ roles/users/API tokens/ACL
→ effective permissions + real API auth
→ optional template source preparation
→ builder/provision/reboot verification
→ guest cleanup
→ seal template
→ final template contract
→ local tooling/status
→ final state
```

## 7. Preflight и safety

Host preflight проверяет:

- root;
- Proxmox VE 9.x;
- Debian trixie;
- `/dev/kvm`;
- `vmbr0`;
- `local` и `local-lvm`;
- обязательные PVE CLI tools.

Состояние `9000` проверяется сразу после configuration snapshot и до остальных host-side изменений. Foreign VM/LXC, unfinished builder, incompatible template, неожиданный origin, dirty Git checkout, потерянный credential, нарушенный source ownership или нестандартная repository configuration приводят к STOP вместо destructive overwrite.

Исходящие HTTP/HTTPS sanity-checks используют connection и total timeout, а canonical SSH transport работает в `BatchMode` с bounded connect/server-alive policy, чтобы сетевой сбой не превращался в неограниченное ожидание.

## 8. Configuration snapshot и state

Перед значимыми host-side изменениями создаётся:

```text
/var/backups/proxmox-configuration/YYYYMMDD-HHMMSS/
```

Это snapshot конфигурации/diagnostics, а не backup VM.

Runtime state:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── bootstrap-complete
```

`state.json`, `last-run.json` и `version` обновляются атомарно через temporary file + `mv`, чтобы interrupt не оставлял частично записанный JSON.

## 9. APT repository policy

PVE no-subscription:

```text
http://download.proxmox.com/debian/pve
trixie
pve-no-subscription
```

Enterprise repository отключается консервативно. Если целевой `.disabled` уже существует с тем же содержимым, active copy удаляется. Если `.disabled` существует с другим содержимым, выполняется STOP — backup не перезаписывается молча.

Ceph enterprise stanza переводится на тот же `ceph-*` no-subscription release только для ожидаемой стандартной конфигурации. Нестандартная схема вызывает STOP.

Полный `apt full-upgrade` выполняется только с `--update-system`.

## 10. Storage и Debian 13 LXC appliance

Additive storage policy:

```text
local:
  snippets
  vztmpl

local-lvm:
  images
  rootdir
```

LXC appliance lifecycle:

```text
найти local Debian 13 template
→ попытаться pveam update

catalog доступен
→ выбрать newest Debian 13
→ скачать при необходимости
→ удалить старые Debian 13 caches

catalog временно недоступен, local template есть
→ WARNING
→ использовать local

catalog недоступен и local template отсутствует
→ STOP
```

Guest manifests используют family selector:

```text
local:vztmpl/debian-13-standard
```

## 11. Постоянный runtime и pvedeploy

Основные каталоги:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/log/proxmox-deployer/
/var/backups/proxmox-configuration/
/var/backups/proxmox-secrets/
```

Если `pvedeploy` уже существует, проверяется contract:

```text
system UID < 1000
primary group = pvedeploy
home = /var/lib/pvedeploy
shell = /bin/bash
```

Несовместимый существующий user не исправляется молча и вызывает STOP.

Разделение ownership:

```text
root
→ canonical Git repo + .git
→ GitHub Deploy Key / SSH config / known_hosts
→ parent /var/lib/proxmox-deployer
→ orchestration state

pvedeploy
→ /var/lib/pvedeploy
→ /var/lib/proxmox-deployer/cache
→ /var/log/proxmox-deployer/audit
→ PVE guest SSH identity
→ host-deploy token read access через root:pvedeploy 0640
```

PVE guest SSH identity:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Именно этот guest-side key остаётся `pvedeploy`-owned. Canonical GitHub Deploy Key ему больше не выдаётся. Private keys не ротируются автоматически.

## 12. PVE identities и ACL

Host/human tooling:

```text
deployer@pve!host-deploy
```

AI / Proximo:

```text
ai-agent@pve!infra
```

Role/ACL policy остаётся additive-only. Новый token создаётся с `privsep=1`; существующий `privsep=0` автоматически не ограничивается.

Новый API token считается завершённо созданным только после атомарного сохранения одноразового secret. Если parsing или запись secret не удались, токен, созданный именно текущим run, удаляется через `pveum user token delete`. Пока secret не подтверждён на диске, token помечен как pending; обычный error/INT/TERM/EXIT cleanup также пытается откатить только этот новый token. Уже существующие tokens никогда не попадают под этот rollback.

Effective permissions проверяются через:

```text
pveum user token permissions <userid> <tokenid>
```

Затем token+secret проходят реальную локальную API authentication check.

## 13. Debian VM template 9000

Target:

```text
VMID: 9000
name: tpl-debian13
Template-Version: 6
root password: locked
root SSH: public-key only
protection: 1
```

Template pipeline встроен в PVE Configuration:

```text
60-template-contract.sh
→ state + full host-visible contract

61-template-source.sh
→ capacity/source
→ image + SHA512SUMS
→ SHA-512
→ production Cloud-Init renderer

62-template-build.sh
→ VM builder
→ QGA/Cloud-Init/bootstrap
→ verification reboot
→ normalized guest exec
→ cleanup assertions
→ standard Cloud-Init
→ qm template
→ protection=1
```

Canonical renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Guest assets:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

Host-visible contract проверяет:

```text
name=tpl-debian13
template=1
ostype=l26
cpu=host
sockets=1
cores=1
memory=1024
scsihw=virtio-scsi-single
scsi0 на local-lvm + discard/iothread/ssd + size>=16G
ide2 = local-lvm:vm-9000-cloudinit + media=cdrom
net0 VirtIO / bridge=vmbr0
boot from scsi0
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

Проверка exact Cloud-Init volume нужна, чтобы обычный ISO/CD-ROM на `ide2` не мог формально пройти contract только из-за `media=cdrom`.

`template_guest_exec` принимает plain-output CLI variants и structured QGA results. Structured result с `pid`/`exited=0`, отсутствующим подтверждённым `exitcode`, ненулевым exitcode или signal считается ошибкой; timeout не может быть принят за успешный guest stdout.

Guest cleanup fail-closed подтверждает отсутствие `debian`, machine-id, SSH host keys, `/root/.ssh`, build-state и builder scripts до `qm template`.

Unfinished builder намеренно сохраняется для диагностики. Foreign/incompatible VMID 9000 не удаляется автоматически.

## 14. Download и temporary artefacts

Debian image download использует retries, connection timeout и low-speed timeout. Temporary downloads имеют уникальные имена и удаляются trap-ом; stale temp files предыдущего interrupted run очищаются под общей orchestration lock.

Temporary Cloud-Init snippet без builder VM считается orphaned build artefact и может быть пересоздан. При существующей unfinished builder VM автоматического cleanup нет.

## 15. CI

Repository checks включают:

```text
scripts/validate_repo.py
Python py_compile
bash -n
ShellCheck
production Cloud-Init renderer
cloud-init schema
Template contract unit tests
Guest exec result/timeout unit tests
API token rollback unit tests
negative malformed guest directory test
git diff --check
```

`validate_repo.py` рассматривает каждый `guests/*/guest.yaml`; каталог, не соответствующий `NNN-name`, является ошибкой, а не молча пропускается.

CI не заменяет реальный PVE integration test. По принятой policy после существенного изменения guest/template assets требуется clean template build + Full Clone smoke-test.

## 16. Status

Stable status command:

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

> Один configuration run = одна точная private Git revision, один общий orchestration lock и один чистый root-trusted source tree. `pvedeploy` может использовать проект для deployment, но не может изменять код или Git credential, которые затем использует `root`. Неоднозначный state, local drift или нарушенная trust boundary приводят к STOP.
