# Файловая структура Public Bootstrap / PVE Configuration

Парный документ: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Public Bootstrap
→ временный runtime только для первоначального доступа к private repo
→ /var/lib/proxmox-bootstrap после успешного первого запуска удаляется

PVE Configuration
→ создаёт и обслуживает постоянную инфраструктурную структуру
→ state хранится в /var/lib/proxmox-deployer/state
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

Назначение:

```text
github_proxmox_repo_ed25519
→ временный read-only GitHub Deploy Key

github_proxmox_repo_ed25519.pub
→ public half для GitHub Deploy keys

known_hosts + ssh_config
→ SSH transport к GitHub

private-repo/
→ temporary shallow checkout zsergeyru/proxmox
→ нужен только для первого запуска PVE Configuration
```

После успешной PVE Configuration Public Bootstrap удаляет `/var/lib/proxmox-bootstrap` и записывает:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

## 2. PVE Configuration source

Канонический private entrypoint:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
```

Модули:

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

Канонический постоянный checkout:

```text
/var/lib/proxmox-deployer/repo/
```

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
        ├── setup/
        │   ├── configure-pve.sh
        │   └── lib/
        └── deploy-guest.py
```

Отдельного `scripts/pve/create-template.sh` больше нет: создание VMID `9000` является частью PVE Configuration. `deploy-guest.py` появляется после реализации deployer.

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
→ host-side SSH identity для доступа PVE tooling к управляемым VM/LXC

ssh/config + known_hosts
→ SSH transport private Git checkout

secrets/host-deploy.token
→ credential deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ credential ai-agent@pve!infra
```

## 4. Права доступа

Рекомендуемая модель:

```text
/etc/proxmox-deployer/
→ root:root

/etc/proxmox-deployer/secrets/
→ root:pvedeploy
→ 0710

host-deploy.token
→ root:pvedeploy
→ 0640

ai-agent-infra.token
→ root:root
→ 0600

ssh/github_proxmox_repo_ed25519
→ pvedeploy:pvedeploy
→ 0600

ssh/github_proxmox_repo_ed25519.pub
→ pvedeploy:pvedeploy
→ 0644

ssh/pve_guest_ed25519
→ pvedeploy:pvedeploy
→ 0600

ssh/pve_guest_ed25519.pub
→ pvedeploy:pvedeploy
→ 0644
```

Правила:

- secrets создаются с безопасным `umask`;
- private keys и token secrets не попадают в Git;
- secrets не выводятся в обычные logs;
- потеря local token secret при существующем PVE token требует явного recovery/rotation;
- потеря `pve_guest_ed25519` не вызывает автоматическую ротацию;
- `pvedeploy` не получает `ai-agent-infra.token`.

## 5. Mutable runtime

```text
/var/lib/proxmox-deployer/
├── repo/
├── state/
└── cache/
```

Назначение:

```text
repo/
→ canonical private source checkout

state/
→ configuration state и revision metadata

cache/
→ безопасно пересоздаваемый cache
```

Private SSH keys и token secrets здесь не хранятся.

Template source image cache расположен штатно в:

```text
/var/lib/vz/template/cache/debian13/
```

Temporary Cloud-Init builder snippet создаётся в storage `local:snippets` только на время сборки VMID `9000` и удаляется после успешного перехода к стандартному Proxmox Cloud-Init.

## 6. State

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
→ timestamp/result/private revision без secrets

last-revision
→ revision canonical private checkout

bootstrap-complete
→ первоначальный Public Bootstrap успешно завершён
```

State не является source of truth: каждый запуск перепроверяет реальное состояние PVE и credentials.

## 7. Logs

```text
/var/log/proxmox-deployer/
├── configure-pve.log
├── deploy-guest.log
└── audit/
```

Создание template 9000 пишет в тот же `configure-pve.log`; отдельного template-builder log/orchestrator нет.

Правила:

- persistent log хранится без ANSI color codes;
- не писать token secrets/private keys;
- логировать итоговые операции и warnings;
- логировать private repo revision;
- не делать полный dump environment с credentials.

## 8. Configuration snapshots

```text
/var/backups/proxmox-configuration/
└── YYYYMMDD-HHMMSS/
```

Это snapshot host-конфигурации перед значимыми изменениями, а не VM backup.

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

## 9. Backup infrastructure secrets

Защищённая локальная область:

```text
/var/backups/proxmox-secrets/
```

Внешняя backup policy должна включать как минимум:

```text
/etc/proxmox-deployer/secrets/host-deploy.token
/etc/proxmox-deployer/secrets/ai-agent-infra.token
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519.pub
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
```

Локальная копия на том же SSD не считается полноценным disaster-recovery backup.

## 10. Стабильные команды

```text
/usr/local/sbin/
├── pve-configuration-status
└── deploy-guest
```

`pve-configuration-status` читает:

```text
/var/lib/proxmox-deployer/state/state.json
```

`deploy-guest` после реализации является небольшой wrapper-командой к source из canonical private checkout:

```text
/usr/local/sbin/deploy-guest
→ /var/lib/proxmox-deployer/repo/scripts/pve/deploy-guest.py
```

## 11. Итоговое дерево

После успешного первоначального bootstrap временного `/var/lib/proxmox-bootstrap` в итоговом дереве нет:

```text
/
├── etc/
│   └── proxmox-deployer/
│       ├── config.yaml
│       ├── ssh/
│       │   ├── github_proxmox_repo_ed25519
│       │   ├── github_proxmox_repo_ed25519.pub
│       │   ├── pve_guest_ed25519
│       │   ├── pve_guest_ed25519.pub
│       │   ├── config
│       │   └── known_hosts
│       └── secrets/
│           ├── host-deploy.token
│           └── ai-agent-infra.token
│
├── var/
│   ├── lib/
│   │   └── proxmox-deployer/
│   │       ├── repo/
│   │       ├── state/
│   │       │   ├── state.json
│   │       │   ├── version
│   │       │   ├── last-run.json
│   │       │   ├── last-revision
│   │       │   └── bootstrap-complete
│   │       └── cache/
│   ├── log/
│   │   └── proxmox-deployer/
│   │       ├── configure-pve.log
│   │       └── audit/
│   └── backups/
│       ├── proxmox-configuration/
│       └── proxmox-secrets/
│
└── usr/local/sbin/
    ├── pve-configuration-status
    └── deploy-guest
```

Главный принцип:

> Public Bootstrap использует отдельную временную область только для первоначального доступа к private source of truth. PVE Configuration владеет постоянным runtime, credentials, state, logs, configuration snapshots и host-side template pipeline.
