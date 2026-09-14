# Bootstrap — public zero-day и private source of truth

## Статус

Каноническая **целевая** архитектура определена в [`20-pve-initialization.md`](20-pve-initialization.md) и [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md).

```text
PUBLIC zsergeyru/proxmox-bootstrap
└── init-pve.sh                  # zero-day entrypoint

PRIVATE zsergeyru/proxmox
├── scripts/pve/create-template.sh
├── scripts/pve/deploy-guest.py
├── guests/*/guest.yaml
└── остальной desired state
```

Public repo не должен становиться вторым source of truth инфраструктуры.

## Текущее переходное состояние

`init-pve.sh` и private `scripts/pve/*` ещё не реализованы полностью. Поэтому public `proxmox-bootstrap` временно содержит рабочие legacy/transitional scripts для template и AI Control:

```text
create-template.sh
create-ai-control-vm.sh
prepare-ai-control.sh
install-ai-agent.sh
bootstrap-ai-control.sh
```

Они остаются рабочими до переноса соответствующей host-side логики в private repo, но **не меняют целевую архитектуру**.

После появления private builder/deployer дублирующая public реализация должна быть удалена либо оставлена только как явно совместимая thin wrapper без собственной deploy logic.

## Целевая zero-day цепочка

```text
чистый PVE
→ public init-pve.sh
→ preflight/packages
→ PVE read-only GitHub Deploy Key
→ read-only checkout private zsergeyru/proxmox
→ зафиксировать private repo commit SHA
→ private scripts/pve/create-template.sh
→ template 9000
→ private deploy-guest
→ guest.yaml
→ PLAN
→ --apply
```

Создание PVE identities/tokens/pool и хранение обоих API token secrets описано в `20/21`.

PVE-side deploy не зависит от `301`, Hermes или Proximo.

## Git на PVE

`git` на PVE является разрешённым bootstrap/runtime client:

```text
read-only Deploy Key
→ git clone/fetch zsergeyru/proxmox
→ /var/lib/proxmox-deployer/repo
```

Это исключение из правила «не превращать PVE в build/dev host». Docker, AI runtimes и application services на PVE по-прежнему не устанавливаются.

## Воспроизводимость

Production/recovery path нельзя запускать с floating `main`.

Подробная policy: [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md).

Текущий проверенный transitional public bootstrap baseline:

```text
ef0e3f21532c6e0fe19b79ea83f5c9b8d420d1f2
```

`bootstrap-ai-control.sh` требует полный `BOOTSTRAP_REF` и использует тот же SHA для всех дочерних stages.

Для целевого private deploy действует аналогичное правило:

```text
fetch
→ выбрать revision
→ PLAN по revision X
→ APPLY по тому же revision X
```

## Template

Текущая переходная реализация public `create-template.sh` создаёт:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 5
```

v5 фиксирует:

- конкретный Debian cloud build;
- SHA-512 verification;
- timestamped Debian APT snapshot для build;
- обычные live Debian repositories восстанавливаются до seal template.

Целевое расположение builder после миграции:

```text
private zsergeyru/proxmox/scripts/pve/create-template.sh
```

## AI Control

После базового host deploy:

```text
301-ai-control
→ prepare-ai-control.sh
→ install-ai-agent.sh --agent hermes
```

Текущий transitional AI bootstrap также pinned:

- Docker packages — exact versions;
- Proximo — exact Python package version;
- Hermes — exact upstream Git commit.

AI Git key и PVE read-only Git key — разные identities.

## `deploy-guest`

Целевая команда:

```bash
deploy-guest 311
```

по умолчанию строит PLAN из private checkout. Реальное изменение требует:

```bash
deploy-guest 311 --apply
```

`guest.yaml` schema и deployability: [`30-guest-manifest.md`](30-guest-manifest.md).

## Live-test gate

Новая bootstrap версия считается проверенной только после:

```text
CI
→ clean PVE/template build
→ Full Clone smoke test
→ manifest PLAN/APPLY smoke test после реализации deployer
```

Для изменений, влияющих на Docker-LXC, применяется дополнительный restore gate из [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md).

## Security

Public repo не содержит secrets.

PVE private Deploy Key создаётся локально и read-only. Token secrets, private SSH keys и AI/provider credentials хранятся по принятой filesystem/backup policy и никогда не входят в Git/version lock.
