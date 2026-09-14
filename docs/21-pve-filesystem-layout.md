# Файловая структура PVE bootstrap/deployer

Этот документ фиксирует каноническое размещение файлов `init-pve.sh`, PVE-side deployer, private Git checkout, обоих PVE API token secrets, runtime state, logs и bootstrap snapshots.

Он является парным документом к [`20-pve-initialization.md`](20-pve-initialization.md). Для PVE bootstrap эти два документа определяют каноническую public/private границу и файловую модель.

Основной принцип:

```text
/etc
→ постоянная конфигурация и secrets

/var/lib
→ private Git checkout и mutable runtime/state

/var/log
→ logs/audit

/var/backups
→ bootstrap snapshots и защищённые backup secrets

/usr/local/sbin
→ стабильные команды/entrypoints

/root
→ только локальная копия zero-day init script
```

---

## 1. Каноническая граница public/private

Для нового PVE существует одна public zero-day entrypoint:

```text
PUBLIC zsergeyru/proxmox-bootstrap
└── init-pve.sh
```

После получения read-only Git access весь основной PVE deploy tooling берётся из private source of truth:

```text
PRIVATE zsergeyru/proxmox
├── scripts/pve/create-template.sh
├── scripts/pve/deploy-guest.py
├── guests/*/guest.yaml
├── templates/
├── ansible/
└── docs/
```

Для PVE bootstrap не должно существовать двух канонических копий `create-template.sh` или `deploy-guest` в public и private repositories одновременно.

Публичный `init-pve.sh` после появления private checkout вызывает код из зафиксированного revision `zsergeyru/proxmox`.

Существующие публичные AI/bootstrap-скрипты старой переходной схемы могут использоваться отдельно для миграции `301`, но не являются source of truth host-side PVE deployer.

---

## 2. Постоянная конфигурация deployer

Канонический каталог:

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
    └── ai-agent-infra.token
```

Назначение:

```text
config.yaml
→ настройки PVE deployer: repo, branch, пути, identities/tokens,
  managed pool и другие постоянные параметры

ssh/github_proxmox_repo_ed25519
→ private read-only GitHub Deploy Key для zsergeyru/proxmox

ssh/github_proxmox_repo_ed25519.pub
→ public часть того же key

ssh/config
→ SSH config для GitHub transport

ssh/known_hosts
→ проверенный GitHub host key

secrets/host-deploy.token
→ token id + secret deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ token id + secret ai-agent@pve!infra
```

### Почему оба token secrets остаются на PVE

Оба credentials принадлежат PVE infrastructure layer и создаются host-side bootstrap.

```text
host-deploy.token
→ нужен обычному PVE deployer

ai-agent-infra.token
→ recovery/source copy для AI infrastructure credential
```

При создании/восстановлении `301-ai-control` рабочая копия credential `ai-agent@pve!infra` передаётся из:

```text
/etc/proxmox-deployer/secrets/ai-agent-infra.token
```

в runtime-файл внутри `301`:

```text
/etc/ai-control/secrets/proxmox-infra.token
```

После передачи host-side copy **не удаляется**.

Это позволяет пересоздать `301` без обязательной ротации API token, если PVE и его защищённый secret backup сохранились.

---

## 3. Права на `/etc/proxmox-deployer`

Нужно разделять обычный host deploy credential и recovery credential AI control.

Рекомендуемая модель:

```text
/etc/proxmox-deployer/
→ root:root

/etc/proxmox-deployer/secrets/
→ root:pvedeploy
→ mode 0710

host-deploy.token
→ root:pvedeploy
→ mode 0640

ai-agent-infra.token
→ root:root
→ mode 0600
```

Таким образом `pvedeploy` может читать только credential, необходимый `deploy-guest`, но не получает AI infrastructure token.

SSH identity PVE принадлежит runtime, выполняющему private Git fetch:

```text
ssh/github_proxmox_repo_ed25519
→ pvedeploy:pvedeploy
→ mode 0600

ssh/github_proxmox_repo_ed25519.pub
→ pvedeploy:pvedeploy
→ mode 0644
```

`ssh/config` и `known_hosts` должны иметь права, позволяющие `pvedeploy` использовать их, но не изменяться посторонними пользователями.

Общие правила:

- перед созданием secrets использовать `umask 077`;
- token secrets и private SSH keys никогда не попадают в Git;
- secrets не записываются в `state.json`;
- secrets не выводятся в обычные logs;
- rotation выполняется только явно;
- потеря host-side secret при существующем PVE token не должна приводить к молчаливому созданию нового credential.

---

## 4. Private Git checkout и deployer runtime

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
│   └── pve/
├── docs/
├── ansible/
└── ...
```

Именно отсюда PVE читает:

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
- перед PLAN фиксируется конкретный commit SHA;
- PLAN и APPLY должны работать по одному и тому же revision.

Public `main` bootstrap repo не используется как второй источник builder/deployer после появления private checkout.

### `state/`

Хранит runtime state самого deployer, например последний применённый/проверенный revision и данные для безопасного продолжения операции.

Secrets здесь не хранятся.

### `cache/`

Только воспроизводимый временный cache. Его удаление не должно уничтожать source of truth или credentials.

---

## 5. Bootstrap state

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
→ timestamp, result, repo revision и другие несекретные метаданные
```

State может содержать факт существования identity/token/secret-файла, но никогда не значение secret.

Это не source of truth: при повторном запуске фактическое состояние PVE, secret files, Git checkout и template перепроверяется.

Secrets в `/var/lib/proxmox-bootstrap/` не хранятся.

---

## 6. Логи

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
- логировать используемый private repo commit SHA;
- логировать итог PLAN/APPLY;
- audit должен позволять понять, какой VMID и какой revision применялся;
- rotation/recovery credential логируется как событие без значения secret.

---

## 7. Bootstrap snapshots конфигурации PVE

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

---

## 8. Backup infrastructure secrets

Для защищённой копии host-side infrastructure credentials выделяется отдельная область:

```text
/var/backups/proxmox-secrets/
```

В backup должны входить **оба API token secrets** и PVE GitHub SSH identity:

```text
/etc/proxmox-deployer/secrets/host-deploy.token
/etc/proxmox-deployer/secrets/ai-agent-infra.token
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519.pub
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
```

Host-side backup secrets является чувствительным объектом и должен быть защищён не хуже исходных credentials.

Локальный `/var/backups` на том же SSD не является достаточным disaster-recovery backup. Должна существовать внешняя защищённая копия согласно общей backup policy проекта.

### Взаимосвязь с backup `301`

Backup `301-ai-control` также чувствителен, потому что содержит runtime copy:

```text
/etc/ai-control/secrets/proxmox-infra.token
```

Но backup `301` **не заменяет** host-side backup `ai-agent-infra.token`.

Каноническая модель:

```text
PVE secret backup
→ infrastructure/recovery source

301 backup
→ runtime/control-plane recovery
```

При потере `301`, но сохранённом PVE secret, тот же credential `ai-agent@pve!infra` можно повторно передать в новую VM.

---

## 9. Стабильные команды

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

Wrapper должен работать с зафиксированным revision и не скачивать при каждом запуске независимую public-копию deployer.

---

## 10. Public zero-day script

После скачивания public entrypoint допускается хранить как:

```text
/root/init-pve.sh
```

Он нужен для zero-day bootstrap/recovery и не является основным runtime deployer после завершения initialization.

Каноническая целевая структура публичного bootstrap repo для PVE:

```text
zsergeyru/proxmox-bootstrap/
├── init-pve.sh
└── README.md
```

После появления read-only доступа к private repo:

```text
init-pve.sh
→ /var/lib/proxmox-deployer/repo/scripts/pve/create-template.sh
→ /var/lib/proxmox-deployer/repo/scripts/pve/deploy-guest.py
```

Builder/deployer не копируются в public repo как второй source of truth.

---

## 11. Итоговое дерево PVE

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
│           └── ai-agent-infra.token
│
├── var/
│   ├── lib/
│   │   ├── proxmox-deployer/
│   │   │   ├── repo/
│   │   │   │   ├── guests/
│   │   │   │   ├── templates/
│   │   │   │   ├── scripts/pve/
│   │   │   │   ├── ansible/
│   │   │   │   └── docs/
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

---

## 12. Главный принцип

> Public repo даёт только zero-day вход в новый PVE. После получения read-only доступа source of truth для builder/deployer находится в private `zsergeyru/proxmox`. Конфигурация и оба API token secrets живут в `/etc`, private Git checkout и mutable runtime — в `/var/lib`, logs — в `/var/log`, backup — в `/var/backups`, стабильные команды — в `/usr/local/sbin`.

И отдельно:

> `deployer@pve!host-deploy` и `ai-agent@pve!infra` имеют разные роли и права, но **оба token secrets постоянно сохраняются на PVE** и входят в защищённый infrastructure backup.
