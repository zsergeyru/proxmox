# Инициализация нового Proxmox VE host

## Статус решения

Используются два компонента с разными обязанностями:

```text
Public Bootstrap
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=8

PVE Configuration
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=17
```

`Public Bootstrap` отвечает за безопасное получение/обновление private source of truth, выбор точной Git revision и handoff. `PVE Configuration` является единственным host-side orchestrator и повторяемо приводит Proxmox VE к ожидаемому состоянию проекта, включая template `9000`.

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
→ temporary shallow checkout
→ определить exact HEAD
→ PVE_CONFIGURATION_SOURCE_REVISION=<HEAD>
→ PVE Configuration
→ permanent runtime
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

## 4. Permanent refresh без потери local drift

Для rerun Public Bootstrap сначала проверяет permanent runtime и origin canonical checkout:

```text
/var/lib/proxmox-deployer/repo
origin = git@github.com:zsergeyru/proxmox.git
```

Затем обязательна clean-check:

```text
tracked drift отсутствует
staged drift отсутствует
untracked drift отсутствует
ignored drift отсутствует
```

Если обнаружено локальное изменение, Bootstrap делает STOP. `reset --hard` и `clean` не используются как способ молча стереть локальный drift.

Только для clean checkout разрешено:

```text
fetch main
→ reset --hard FETCH_HEAD
→ clean -ffd
→ повторная clean-check
→ определить SHA
→ handoff
```

## 5. Одна Git revision на один run

Public Bootstrap передаёт:

```text
PVE_CONFIGURATION_SOURCE_REVISION=<40-char SHA>
```

До загрузки `lib/*.sh` PVE Configuration проверяет сам source checkout:

```text
Git root ожидаемый
HEAD == source revision
worktree полностью clean, включая ignored files
```

Только после этого source-модули становятся исполняемым кодом текущего run.

Canonical private checkout после sync обязан иметь тот же SHA. Если `main` изменился между временным checkout и canonical fetch/clone, run останавливается вместо смешивания revisions.

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
│   └── test-template-contract.sh
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
shared lock + source SHA/clean check
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
→ config.yaml + SSH identities
→ canonical GitHub access
→ canonical checkout exact source revision
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

Состояние `9000` проверяется сразу после configuration snapshot и до остальных host-side изменений. Foreign VM/LXC, unfinished builder, incompatible template, неожиданный origin, dirty Git checkout, потерянный credential или нестандартная repository configuration приводят к STOP вместо destructive overwrite.

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

PVE guest SSH identity:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Private key не ротируется автоматически.

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
ide2 Cloud-Init CD-ROM на local-lvm
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

> Один configuration run = одна точная private Git revision, один общий orchestration lock и один чистый source tree. Неоднозначный state, local drift или потенциально разрушительное автоматическое исправление приводят к STOP, а не к молчаливой перезаписи.
