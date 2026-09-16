# Файловая структура Public Bootstrap / PVE Configuration

Парный документ: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Public Bootstrap
→ временный runtime только для первоначального доступа к private repo
→ /var/lib/proxmox-bootstrap после успешного first run удаляется

PVE Configuration
→ создаёт и обслуживает постоянную инфраструктурную структуру
→ state хранится в /var/lib/proxmox-deployer/state

оба компонента
→ используют /run/lock/proxmox-orchestration.lock
```

## 1. Public Bootstrap

Публичный репозиторий:

```text
zsergeyru/proxmox-bootstrap
└── bootstrap-pve.sh
```

До первого успешного handoff используется временная область:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Каталог создаётся как `root:root 0700`.

`private-repo/` является disposable checkout. При resume он может быть приведён к свежему `FETCH_HEAD` через `reset --hard` и `git clean -ffdx`, включая ignored cache/build artifacts. Это правило относится только к temporary runtime.

После успешной PVE Configuration Public Bootstrap удаляет `/var/lib/proxmox-bootstrap` и записывает:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

## 2. PVE Configuration source

Канонический private entrypoint:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
```

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

Канонический постоянный checkout:

```text
/var/lib/proxmox-deployer/repo/
```

Он является runtime-copy private source of truth. Перед автоматическим Git refresh проверяется полный clean state; local tracked/staged/untracked/ignored drift не стирается молча. В отличие от temporary checkout, `git clean -ffdx` здесь не применяется.

Пример:

```text
/var/lib/proxmox-deployer/repo/
├── docs/
├── guests/
├── templates/
│   └── debian13/
│       ├── README.md
│       ├── build-policy.md
│       ├── cloud-init.yaml
│       ├── template-bootstrap.sh
│       └── template-finalize.sh
├── ansible/
└── scripts/
    └── pve/
        └── setup/
            ├── configure-pve.sh
            ├── render-template-cloud-init.py
            ├── tests/
            └── lib/
```

Отдельного `scripts/pve/create-template.sh` нет. `deploy-guest.py` появится после реализации deployer.

## 3. Общая orchestration lock

```text
/run/lock/proxmox-orchestration.lock
```

Public Bootstrap открывает lock на fd 9 и передаёт этот fd PVE Configuration. Прямой `configure-pve.sh` берёт тот же lock самостоятельно.

Lock защищает одновременно:

```text
canonical private checkout
permanent runtime mutation
host configuration
Template 9000 pipeline
state/final marker transition
```

## 4. Постоянная конфигурация deployer

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
→ host-side SSH identity для управляемых VM/LXC

ssh/config + known_hosts
→ SSH transport private Git checkout

secrets/host-deploy.token
→ credential deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ credential ai-agent@pve!infra
```

Canonical SSH config включает strict host-key checking, `BatchMode yes`, bounded `ConnectTimeout` и server-alive policy.

## 5. Права и Linux runtime user

```text
/etc/proxmox-deployer/
→ root:root

/etc/proxmox-deployer/secrets/
→ root:pvedeploy 0710

host-deploy.token
→ root:pvedeploy 0640

ai-agent-infra.token
→ root:root 0600

GitHub/PVE guest private keys
→ pvedeploy:pvedeploy 0600
```

`pvedeploy` contract:

```text
system UID < 1000
primary group = pvedeploy
home = /var/lib/pvedeploy
shell = /bin/bash
```

Если существующий user не соответствует contract, PVE Configuration не меняет его автоматически.

Secrets создаются с безопасным `umask`, не попадают в Git и не выводятся в обычные logs. Для нового API token one-time secret сначала записывается во временный файл, owner/mode выставляются до atomic `mv`. До успешного rename token считается pending; обычный failure/interruption пытается удалить только этот вновь созданный token. Потеря уже существующего token secret/private key требует явного recovery/rotation.

## 6. Mutable runtime

```text
/var/lib/proxmox-deployer/
├── repo/
├── state/
└── cache/
```

Private SSH keys и token secrets здесь не хранятся.

Template image cache:

```text
/var/lib/vz/template/cache/debian13/
```

Temporary Cloud-Init builder snippet создаётся в `local:snippets` только на время сборки 9000. Если VMID свободен, orphan snippet предыдущего interrupted run может быть пересоздан. При существующей unfinished builder VM автоматический cleanup не выполняется.

Temporary image downloads используют скрытые уникальные файлы рядом с target и очищаются trap-ом/stale cleanup под общей lock.

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
→ текущее состояние PVE Configuration

version
→ PVE_CONFIGURATION_VERSION

last-run.json
→ timestamp/result/source revision без secrets

last-revision
→ revision canonical private checkout

bootstrap-complete
→ первоначальный Public Bootstrap успешно завершён
```

`state.json`, `last-run.json` и `version` записываются атомарно через temporary file + `mv`. Source revision известна уже в `running` state и сохраняется также при early failure/interruption.

State не является source of truth: каждый запуск перепроверяет PVE, credentials и Git state.

## 8. Logs

```text
/var/log/proxmox-deployer/
├── configure-pve.log
└── audit/
```

Создание template 9000 пишет в тот же `configure-pve.log`; отдельного template-builder log/orchestrator нет.

Правила:

- persistent log без ANSI color codes;
- не писать token secrets/private keys;
- логировать operations/warnings/revision;
- не делать полный environment dump.

## 9. Configuration snapshots

```text
/var/backups/proxmox-configuration/YYYYMMDD-HHMMSS/
```

Это snapshot host configuration/diagnostics перед значимыми изменениями, а не VM backup.

Типичное содержимое:

```text
files/
├── etc/network/interfaces
├── etc/hosts
├── etc/hostname
├── etc/resolv.conf
├── etc/pve/storage.cfg
├── etc/pve/user.cfg
├── etc/pve/datacenter.cfg
└── etc/apt/...

diagnostics/
├── pveversion.txt
├── storage-status.txt
├── users.txt
├── roles.txt
├── acl.txt
├── pools.txt
├── ip-addr.txt
└── ip-route.txt
```

Не архивировать бездумно весь `/etc/pve`, так как это `pmxcfs`.

## 10. Backup infrastructure secrets

Защищённая локальная область:

```text
/var/backups/proxmox-secrets/
```

Внешняя backup policy должна включать как минимум token secrets, canonical GitHub Deploy Key, PVE guest SSH keypair, SSH config и known_hosts.

Локальная копия на том же SSD не считается полноценным disaster-recovery backup.

## 11. Стабильные команды

```text
/usr/local/sbin/
├── pve-configuration-status
└── deploy-guest
```

`pve-configuration-status` читает `state.json`.

`deploy-guest` после реализации будет небольшой wrapper-командой к source из canonical private checkout.

## 12. Итоговое дерево

```text
/
├── etc/proxmox-deployer/
│   ├── config.yaml
│   ├── ssh/
│   └── secrets/
├── run/lock/
│   └── proxmox-orchestration.lock
├── var/lib/proxmox-deployer/
│   ├── repo/
│   ├── state/
│   └── cache/
├── var/log/proxmox-deployer/
├── var/backups/proxmox-configuration/
├── var/backups/proxmox-secrets/
└── usr/local/sbin/
    ├── pve-configuration-status
    └── deploy-guest
```

Главный принцип:

> Public Bootstrap использует отдельную temporary область только для первоначального доступа. Permanent runtime принадлежит PVE Configuration, а shared orchestration lock и clean Git checks защищают его от параллельного изменения и скрытого local drift.
