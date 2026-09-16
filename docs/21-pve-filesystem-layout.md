# Файловая структура Public Bootstrap / PVE Configuration

Парный документ: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Public Bootstrap
→ временный runtime только для получения/обновления private repo
→ /var/lib/proxmox-bootstrap после успешного первого запуска удаляется

PVE Configuration
→ создаёт и обслуживает постоянную инфраструктурную структуру
→ state хранится в /var/lib/proxmox-deployer/state
```

## 1. Public Bootstrap

Публичный репозиторий:

```text
zsergeyru/proxmox-bootstrap
├── bootstrap-pve.sh   # canonical
└── init-pve.sh        # legacy compatibility loader
```

До первого успешного handoff используется:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Каталог `root:root 0700`. Temporary Deploy Key read-only; private key является source of truth, `.pub` может восстанавливаться из него.

После успешной PVE Configuration:

```text
→ удалить /var/lib/proxmox-bootstrap
→ создать /var/lib/proxmox-deployer/state/bootstrap-complete
```

Legacy `stage0-complete` распознаётся для миграции, но новым canonical marker является только `bootstrap-complete`.

## 2. PVE Configuration source

Canonical private entrypoint:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
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

Legacy `scripts/pve/bootstrap/init-pve.sh` временно остаётся wrapper’ом. `init-pve-core.sh` удалён.

## 3. Постоянная конфигурация deployer

```text
/etc/proxmox-deployer/
├── config.yaml
├── ssh/
│   ├── github_proxmox_repo_ed25519
│   ├── github_proxmox_repo_ed25519.pub
│   ├── pve_guest_ed25519
│   ├── pve_guest_ed25519.pub
│   ├── config
│   └── known_hosts
└── secrets/
    ├── host-deploy.token
    └── ai-agent-infra.token
```

Назначение:

```text
config.yaml
→ постоянные параметры host-side deployer

ssh/github_proxmox_repo_ed25519
→ canonical read-only Deploy Key для zsergeyru/proxmox

ssh/pve_guest_ed25519
→ private key host-side tooling для SSH-доступа к VM/LXC

secrets/host-deploy.token
→ deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ ai-agent@pve!infra
```

`pve_guest_ed25519` автоматически не ротируется. Потеря private key при сохранившемся public key — recovery-ситуация.

## 4. Права

```text
/etc/proxmox-deployer/                 root:root
/etc/proxmox-deployer/secrets/         root:pvedeploy 0710
host-deploy.token                      root:pvedeploy 0640
ai-agent-infra.token                   root:root 0600
ssh/github_proxmox_repo_ed25519        pvedeploy:pvedeploy 0600
ssh/github_proxmox_repo_ed25519.pub    pvedeploy:pvedeploy 0644
ssh/pve_guest_ed25519                  pvedeploy:pvedeploy 0600
ssh/pve_guest_ed25519.pub              pvedeploy:pvedeploy 0644
```

Secrets/private keys не хранятся в Git и не выводятся в обычные logs.

## 5. Private Git checkout

```text
/var/lib/proxmox-deployer/repo/
```

Пример relevant tree:

```text
repo/
├── docs/
├── guests/
├── templates/
├── ansible/
└── scripts/
    └── pve/
        ├── setup/
        │   ├── configure-pve.sh
        │   └── lib/...
        ├── bootstrap/
        │   └── init-pve.sh       # legacy wrapper
        ├── create-template.sh
        └── deploy-guest.py       # после реализации
```

Checkout обновляется read-only из GitHub; полная history не требуется.

## 6. Mutable runtime

```text
/var/lib/proxmox-deployer/
├── repo/
├── state/
└── cache/
```

`repo/` — private source of truth checkout. `state/` — runtime state и revision. `cache/` — пересоздаваемые данные. API secrets/private SSH keys здесь не хранятся.

## 7. State

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── bootstrap-complete
```

Назначение:

```text
state.json
→ состояние PVE Configuration
→ component=pve-configuration
→ configuration_version=13

version
→ версия PVE Configuration contract

last-run.json
→ timestamp/result/private revision без secrets

last-revision
→ revision canonical private checkout

bootstrap-complete
→ первоначальный Public Bootstrap успешно завершён
```

State не является source of truth: PVE Configuration перепроверяет реальный host.

## 8. Логи

```text
/var/log/proxmox-bootstrap/
└── configure-pve.log

/var/log/proxmox-deployer/
├── deploy-guest.log
└── audit/
```

Терминальный вывод PVE Configuration цветной при TTY; `configure-pve.log` сохраняется без ANSI escape-кодов.

Правила: не логировать token secrets/private keys, не делать dump environment с credentials, фиксировать warnings и private repo revision.

## 9. Configuration snapshots

```text
/var/backups/proxmox-bootstrap/
└── YYYYMMDD-HHMMSS/
```

Это snapshot host-конфигурации перед изменениями, а не backup VM. Сохраняются применимые network/hostname/resolver/APT/PVE config files и diagnostics.

## 10. Backup infrastructure secrets

Внешняя backup policy должна включать как минимум:

```text
/etc/proxmox-deployer/secrets/host-deploy.token
/etc/proxmox-deployer/secrets/ai-agent-infra.token
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519(.pub)
/etc/proxmox-deployer/ssh/pve_guest_ed25519(.pub)
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
```

Локальная копия на том же SSD не является полноценным disaster-recovery backup.

## 11. Stable commands

```text
/usr/local/sbin/
├── deploy-guest
├── pve-configuration-status    # canonical
└── pve-bootstrap-status        # legacy wrapper
```

`pve-configuration-status` читает:

```text
/var/lib/proxmox-deployer/state/state.json
```

После реализации `deploy-guest` является wrapper к source в canonical private checkout.

## 12. Итоговое дерево

После успешного первоначального Public Bootstrap:

```text
/
├── etc/proxmox-deployer/
│   ├── config.yaml
│   ├── ssh/...
│   └── secrets/...
├── var/lib/proxmox-deployer/
│   ├── repo/
│   ├── state/
│   │   ├── state.json
│   │   ├── version
│   │   ├── last-run.json
│   │   ├── last-revision
│   │   └── bootstrap-complete
│   └── cache/
├── var/log/proxmox-bootstrap/configure-pve.log
├── var/log/proxmox-deployer/
├── var/backups/proxmox-bootstrap/
├── var/backups/proxmox-secrets/
└── usr/local/sbin/
    ├── deploy-guest
    ├── pve-configuration-status
    └── pve-bootstrap-status
```

`/var/lib/proxmox-bootstrap` после успешного первоначального bootstrap отсутствует.

Главный принцип:

> Public Bootstrap владеет только временным handoff runtime. Постоянные credentials, private checkout, PVE Configuration state и host tooling принадлежат canonical deployer runtime.
