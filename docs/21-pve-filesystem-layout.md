# Файловая структура PVE bootstrap/deployer

Этот документ фиксирует каноническое размещение файлов `init-pve.sh`, PVE-side deployer, private Git checkout, secrets, runtime state, logs и bootstrap snapshots.

Основной принцип:

```text
/etc
→ постоянная конфигурация и secrets

/var/lib
→ Git checkout и runtime/state

/var/log
→ logs

/var/backups
→ bootstrap snapshots и защищённые backup secrets

/usr/local/sbin
→ стабильные команды/entrypoints

/root
→ только zero-day init script
```

## 1. Постоянная конфигурация deployer

```text
/etc/proxmox-deployer/
├── config.yaml
├── ssh/
│   ├── github_proxmox_repo_ed25519
│   ├── github_proxmox_repo_ed25519.pub
│   ├── config
│   └── known_hosts
└── secrets/
    ├── host-deploy.token
    └── ai-agent-proximo.token
```

Назначение:

```text
config.yaml
→ настройки PVE deployer: repo, branch, пути, имена identities/tokens, pool managed и другие постоянные параметры

ssh/github_proxmox_repo_ed25519
→ private read-only GitHub Deploy Key для zsergeyru/proxmox

ssh/github_proxmox_repo_ed25519.pub
→ public часть того же key

ssh/config
→ SSH alias/config для GitHub transport

ssh/known_hosts
→ проверенный host key GitHub

secrets/host-deploy.token
→ secret deployer@pve!host-deploy

secrets/ai-agent-proximo.token
→ secret ai-agent@pve!proximo
```

Правила:

- secrets и private key никогда не попадают в Git;
- перед созданием secret files использовать `umask 077`;
- private key и token files имеют mode `0600`;
- каталоги `ssh/` и `secrets/` имеют mode `0700`;
- `pvedeploy` получает доступ только к тем secrets, которые нужны host-side deploy runtime;
- AI token может оставаться root-readable и передаваться в `301` bootstrap tooling по необходимости.

## 2. Private Git checkout и deployer runtime

```text
/var/lib/proxmox-deployer/
├── repo/
│   └── .git/
├── state/
│   ├── deploy-state.json
│   └── last-revision
└── cache/
```

### `repo/`

Целевой каталог private shallow checkout:

```text
/var/lib/proxmox-deployer/repo/
```

Внутри находится текущее дерево `zsergeyru/proxmox`, например:

```text
repo/
├── guests/
├── templates/
├── scripts/
│   ├── pve/
│   └── ai-control/
├── docs/
├── ansible/
└── ...
```

Именно отсюда PVE читает, например:

```text
guests/311-dev-services/guest.yaml
scripts/pve/create-template.sh
scripts/pve/deploy-guest.py
```

Checkout:

- owner `pvedeploy:pvedeploy`;
- read-only по смыслу проекта;
- PVE не выполняет из него commit/push;
- первая версия использует shallow clone (`depth=1`);
- sparse checkout пока не используется;
- PLAN и APPLY должны работать по одному и тому же зафиксированному commit SHA.

### `state/`

Хранит runtime state самого deployer, например последний применённый/проверенный revision и данные, необходимые для безопасного продолжения операции.

Secrets здесь не хранятся.

### `cache/`

Только воспроизводимый временный cache. Его удаление не должно уничтожать source of truth или credentials.

## 3. Bootstrap state

```text
/var/lib/proxmox-bootstrap/
├── state.json
├── version
└── last-run.json
```

Назначение:

```text
state.json
→ resume/idempotency/progress init-pve

version
→ установленная bootstrap schema/version

last-run.json
→ сведения о последнем запуске: timestamp, result, repo revision и другие несекретные метаданные
```

Это не source of truth: при повторном запуске фактическое состояние PVE/Git/template всегда перепроверяется.

Secrets в `/var/lib/proxmox-bootstrap/` не хранятся.

## 4. Логи

```text
/var/log/proxmox-deployer/
├── deploy-guest.log
├── git-update.log
└── audit/

/var/log/proxmox-bootstrap/
└── init-pve.log
```

Правила logging:

- не писать token secrets;
- не писать private SSH keys;
- не делать полный dump environment, если там могут быть credentials;
- логировать используемый repo commit SHA и итог PLAN/APPLY;
- audit должен позволять понять, какой VMID и какой revision применялся.

## 5. Bootstrap snapshots конфигурации PVE

```text
/var/backups/proxmox-bootstrap/
├── YYYYMMDD-HHMMSS/
└── ...
```

Пример содержимого одного snapshot:

```text
YYYYMMDD-HHMMSS/
├── etc/
│   ├── network-interfaces
│   ├── hosts
│   ├── hostname
│   ├── resolv.conf
│   └── apt/
├── pve/
│   ├── storage.cfg
│   ├── user.cfg
│   └── datacenter.cfg
└── diagnostics/
    ├── pveversion.txt
    ├── storage.txt
    ├── users.txt
    ├── roles.txt
    ├── acl.txt
    └── pools.txt
```

Это snapshot перед изменяющими bootstrap-операциями, а не backup VM и не полный disaster recovery PVE.

Не нужно бездумно архивировать весь `/etc/pve`, потому что это `pmxcfs`.

## 6. Backup infrastructure secrets

Для защищённой копии infrastructure credentials выделяется отдельная область:

```text
/var/backups/proxmox-secrets/
```

В backup должны попадать как минимум необходимые для восстановления PVE bootstrap/deployer identities данные из:

```text
/etc/proxmox-deployer/secrets/
/etc/proxmox-deployer/ssh/
```

Такой backup является чувствительным объектом и должен быть защищён не хуже самих secrets. В дальнейшем предпочтительно иметь внешнюю копию, потому что локальный `/var/backups` на том же системном SSD не помогает при физической потере диска.

## 7. Стабильные команды

Пользовательские/system entrypoints:

```text
/usr/local/sbin/
├── deploy-guest
└── pve-bootstrap-status
```

Правило: большие канонические исходники не дублируются в `/usr/local/sbin`.

Например:

```text
/usr/local/sbin/deploy-guest
→ небольшой стабильный wrapper
→ /var/lib/proxmox-deployer/repo/scripts/pve/deploy-guest.py
```

Source code остаётся в private Git checkout, а `/usr/local/sbin` даёт стабильную команду независимо от физического расположения implementation.

## 8. Public zero-day script

Публичный initial script допускается хранить после скачивания как:

```text
/root/init-pve.sh
```

Он нужен для zero-day bootstrap/recovery и не является основным runtime deployer после завершения initialization.

Публичный `zsergeyru/proxmox-bootstrap` в целевой архитектуре содержит только `init-pve.sh` и сопутствующий README. Остальные executable scripts после получения read-only Git access берутся из private `zsergeyru/proxmox`.

## 9. Итоговое дерево

```text
/
├── etc/
│   └── proxmox-deployer/
│       ├── config.yaml
│       ├── ssh/
│       │   ├── github_proxmox_repo_ed25519
│       │   ├── github_proxmox_repo_ed25519.pub
│       │   ├── config
│       │   └── known_hosts
│       └── secrets/
│           ├── host-deploy.token
│           └── ai-agent-proximo.token
│
├── var/
│   ├── lib/
│   │   ├── proxmox-deployer/
│   │   │   ├── repo/
│   │   │   ├── state/
│   │   │   └── cache/
│   │   └── proxmox-bootstrap/
│   │       ├── state.json
│   │       ├── version
│   │       └── last-run.json
│   │
│   ├── log/
│   │   ├── proxmox-deployer/
│   │   └── proxmox-bootstrap/
│   │
│   └── backups/
│       ├── proxmox-bootstrap/
│       └── proxmox-secrets/
│
├── usr/
│   └── local/
│       └── sbin/
│           ├── deploy-guest
│           └── pve-bootstrap-status
│
└── root/
    └── init-pve.sh
```

## 10. Главный принцип

> Конфигурация и credentials живут в `/etc`, mutable runtime/source cache — в `/var/lib`, logs — в `/var/log`, backup — в `/var/backups`, стабильные команды — в `/usr/local/sbin`, а `/root/init-pve.sh` остаётся только zero-day входной точкой.
