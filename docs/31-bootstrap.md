# Public Bootstrap и PVE Configuration — текущее состояние

## Статус

Проект использует два компонента:

```text
PUBLIC BOOTSTRAP
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
PUBLIC_BOOTSTRAP_VERSION=11

PVE CONFIGURATION
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
PVE_CONFIGURATION_VERSION=21
```

Канонические документы:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md);
- [`25-pve-access-control.md`](25-pve-access-control.md);
- [`30-guest-manifest.md`](30-guest-manifest.md);
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## Public Bootstrap

Канонический запуск:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

Полное обновление Proxmox/Debian только явно:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

Явный Full Clone smoke-test уже существующего template `9000`:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --smoke-test-template
```

Public Bootstrap v11 принимает этот параметр и передаёт его PVE Configuration. После новой сборки `9000` отдельный параметр не нужен: smoke-test запускается автоматически.

### Общая orchestration lock

Public Bootstrap и прямой PVE Configuration используют:

```text
/run/lock/proxmox-orchestration.lock
```

Lock удерживается на всём пути Git refresh → handoff → configuration → marker. Параллельные bootstrap/configuration runs запрещены.

### First run / resume

Чистый first run:

```text
root/PVE check
→ shared lock
→ minimal Git/SSH packages
→ GitHub connectivity
→ temporary read-only Deploy Key
→ temporary root-owned private checkout
→ exact HEAD
→ PVE_CONFIGURATION_SOURCE_REVISION=<HEAD>
→ PVE Configuration
→ permanent root-trusted runtime
→ cleanup temporary bootstrap
→ bootstrap-complete
```

Незавершённый first run продолжает работу с уже созданным permanent runtime и не ротирует существующие credentials. Temporary checkout является disposable и может очищаться `git clean -ffdx`; permanent checkout local drift не стирает.

### Root trust boundary

```text
/var/lib/proxmox-deployer        root:pvedeploy 0750
/var/lib/proxmox-deployer/repo   root-owned, go-w
GitHub private Deploy Key        root:root 0600
Git SSH config                   root:root 0600
```

`pvedeploy` читает project source, но не может менять canonical checkout, `.git` или credential, которые затем использует root.

## Одна private revision на один run

До `source lib/*.sh` PVE Configuration проверяет:

```text
root process
root-owned source tree
нет group/other write
HEAD == expected SHA
tracked/staged/untracked/ignored drift отсутствует
```

Canonical checkout после sync обязан иметь тот же SHA. State текущего run с самого начала содержит source revision.

## PVE Configuration

Структура:

```text
scripts/pve/setup/
├── configure-pve.sh
├── render-template-cloud-init.py
├── tests/
│   ├── test-template-contract.sh
│   ├── test-template-guest-exec.sh
│   ├── test-template-smoke.sh
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
    ├── 63-template-smoke.sh
    └── 70-tooling.sh
```

Основная последовательность:

```text
source trust + shared lock
→ host preflight/snapshot/repos/packages/network/storage
→ pvedeploy runtime + credentials
→ root-owned canonical checkout
→ roles/users/API tokens/ACL
→ template 9000 contract/build при необходимости
→ ASCII builder telemetry
→ verification reboot + cleanup + seal
→ final template contract
→ Full Clone smoke-test при AUTO/FORCE/PENDING
→ tooling/status/final state
```

## `pvedeploy` runtime

```text
root
→ canonical repo/.git
→ canonical GitHub Deploy Key
→ orchestration state

pvedeploy
→ /var/lib/pvedeploy
→ cache/audit
→ PVE guest SSH key
→ host-deploy token read access
```

Этот split сохраняет `pvedeploy` пригодным для будущего `deploy-guest`, но исключает подмену root-executed source.

## Template 9000

```text
VMID: 9000
name: tpl-debian13
Template-Version: 7
ciuser: root
root password: locked
root SSH: public-key only
protection: 1
```

Pipeline:

```text
60-template-contract.sh
→ host-visible contract

61-template-source.sh
→ Debian image + SHA512SUMS + renderer

62-template-build.sh
→ builder
→ QGA/Cloud-Init
→ ASCII stage-code telemetry
→ verification reboot
→ cleanup
→ standard Cloud-Init
→ qm template
→ protection=1

63-template-smoke.sh
→ Full Clone 9000 → 9099
→ scsi0 20G до первого boot
→ injected pve_guest SSH key
→ QGA + Cloud-Init + kernel + SSH + machine-id + SSH host keys + rootfs grow
→ reboot + повторные проверки
→ success: shutdown + destroy 9099
→ failure: сохранить 9099
```

Builder передаёт через QGA только ASCII stage identifiers; русская подпись строится host-side, поэтому mojibake не зависит от QGA transport.

## Smoke state и safety

Host-side state:

```text
/var/lib/proxmox-deployer/state/template-smoke.json
```

Trigger:

```text
9000 создан текущим run
→ AUTO

--smoke-test-template
→ FORCE

template-smoke.json = pending
→ RESUME на следующем обычном run
```

Safety VMID `9099`:

```text
свободен
→ разрешён Full Clone

project smoke residue
→ STOP и сохранить для диагностики

обычная VM/LXC
→ STOP, ничего не менять

полный smoke SUCCESS
→ удалить 9099 и записать passed
```

Smoke проверяет реальную жизнеспособность Full Clone, в том числе root SSH по `/etc/proxmox-deployer/ssh/pve_guest_ed25519`, Cloud-Init `done`, regular Debian kernel, новый `machine-id`, созданные SSH host keys, filesystem grow после `16G→20G`, reboot и стабильность identity после reboot.

## PVE identities и ACL

```text
deployer@pve!host-deploy
→ человек / host-side tooling / будущий deploy-guest

ai-agent@pve!infra
→ AI / Proximo
→ write-zone /pool/managed
→ template 9000 отдельно как clone source
```

Role/ACL policy additive-only. Новый token создаётся с `privsep=1`; существующий `privsep=0` автоматически не ограничивается. Новый one-time token secret сохраняется атомарно, pending creation откатывается при обычном failure/interruption.

## CI

Private repository checks включают:

```text
repository validator
Python compile
bash -n
ShellCheck
production Cloud-Init render + schema
ASCII stage-code contract
Template contract unit tests
Guest exec normalization tests
Template smoke safety/state tests
API token rollback tests
source trust boundary tests
malformed guest directory negative test
whitespace
```

CI проверяет код и safety invariants, но не эмулирует Proxmox. Реальный Full Clone smoke является runtime-этапом самой PVE Configuration.

## Runtime и status

State:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── last-run.json
├── version
├── last-revision
├── template-smoke.json
└── bootstrap-complete
```

Stable status command:

```text
/usr/local/sbin/pve-configuration-status
```

Главный принцип:

> Public Bootstrap отвечает за safe refresh private source of truth и передачу параметров. PVE Configuration является единственным host-side orchestrator. Новый template считается runtime-проверенным только после успешного Full Clone smoke-test; failed smoke VM автоматически не уничтожается.
