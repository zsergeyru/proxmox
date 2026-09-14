# Инициализация нового Proxmox VE host

## Статус решения

Архитектура первой версии bootstrap **принята**.

Нужен один публичный zero-day скрипт:

```text
zsergeyru/proxmox-bootstrap/init-pve.sh
```

После того как новый PVE получает read-only доступ к приватному `zsergeyru/proxmox`, все остальные executable-скрипты, manifests и deploy logic берутся уже из приватного репозитория.

Главная цель:

> После одного публичного `init-pve.sh` и однократной регистрации read-only GitHub Deploy Key новый PVE должен самостоятельно получить private source of truth, создать базовый template и уметь разворачивать VM/LXC по `guest.yaml` без зависимости от `301-ai-control` или конкретного AI-агента.

---

# Исходное состояние

```text
новый компьютер
└── установлен Proxmox VE
```

Не предполагается наличие:

- Git checkout приватного репозитория;
- `301-ai-control`;
- Hermes или другого AI-агента;
- Proximo;
- GitHub credentials;
- template `9000`;
- проектных PVE users/roles/tokens/pools.

---

# Целевое состояние

После инициализации:

```text
PVE
├── корректный pve-no-subscription repository
├── минимальный bootstrap/admin toolset
├── Linux user pvedeploy
├── PVE read-only GitHub Deploy Key
├── shallow read-only checkout zsergeyru/proxmox
├── resource pool managed
├── PVE identity deployer@pve!host-deploy
├── PVE identity ai-agent@pve!proximo
├── локальное защищённое хранилище secrets
├── template 9000 tpl-debian13
├── private deploy tooling из zsergeyru/proxmox
├── bootstrap state/version
└── итоговый health report
```

После этого host-side deploy должен работать независимо от AI:

```bash
deploy-guest 301 --apply
deploy-guest 311 --apply
```

---

# 1. Preflight host — принято

Перед изменениями `init-pve.sh` проверяет:

- запуск от `root`;
- `pveversion`, `qm`, `pct`, `pvesh`, `pveum`, `pvesm`;
- node name;
- аппаратную виртуализацию/KVM;
- наличие и состояние `local` и `local-lvm`;
- наличие `vmbr0`;
- свободное место на root и основных storage;
- DNS resolution;
- доступ к `download.proxmox.com`, GitHub и публичному bootstrap repo;
- фактическое время и синхронизацию;
- отсутствие конфликтов bootstrap VMID, в первую очередь `9000`.

Критическая ошибка останавливает bootstrap до существенных изменений.

---

# 2. Бесплатный Proxmox repository — принято

Для PVE без subscription используется штатный бесплатный repository:

```text
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
```

Также сохраняются корректные Debian base/security repositories.

Правила:

- `pve-test` по умолчанию не включать;
- enterprise repository отключать, если подписки нет;
- сторонние APT repositories не добавлять без отдельного решения;
- перед изменением APT sources сохранить snapshot;
- обычный повторный init не должен автоматически выполнять большой upgrade.

Полное обновление хоста — явный режим:

```bash
init-pve.sh --update-system
```

Обычный init может выполнять `apt update` и устанавливать только требуемые bootstrap packages.

---

# 3. Пакеты — принято

## Обязательный runtime

```text
git
openssh-client
python3
python3-yaml
curl
jq
ca-certificates
```

Они нужны для SSH Deploy Key, private Git checkout, разбора `guest.yaml`, bootstrap/deploy tooling и HTTPS/API diagnostics.

## Небольшой host-admin набор

```text
mc
htop
tmux
smartmontools
lm-sensors
```

## Условные пакеты

`rsync`, `tar`, `unzip`, `lsof`, `ethtool`, `pciutils`, `usbutils` и подобные ставятся только если отсутствуют **и реально нужны** bootstrap/deployer/diagnostics.

На PVE не устанавливать без отдельного решения:

- Docker;
- AI runtimes;
- Hermes;
- application services;
- произвольные development toolchains.

---

# 4. Время и DNS — принято

Проверять:

- timezone;
- синхронизацию времени;
- фактические дату/время;
- DNS resolution;
- доступность GitHub и Proxmox repositories по DNS.

Timezone не менять на захардкоженное значение без явной policy/параметра.

---

# 5. Storage preparation — принято

Обязательные базовые storage:

```text
local
local-lvm
```

Для `local` проверить необходимые content types. Проекту нужны только реально используемые возможности, включая `snippets`, а при необходимости также `iso`, `vztmpl`, `backup`.

При изменении `content` нельзя удалить уже разрешённые типы.

Алгоритм:

```text
прочитать текущий storage config
→ определить только недостающие capabilities
→ показать PLAN
→ добавить недостающее
→ повторно проверить результат
```

---

# 6. `managed` pool — принято

Создаётся project resource pool:

```text
managed
```

Его назначение — **security и organization boundary** для обычной runtime automation.

В `managed` попадают обычные VM/LXC, которыми разрешено штатно управлять автоматизации.

Не включать автоматически в обычную self-managed write-zone:

```text
100   production HAOS
301   AI control plane
320   bootstrap/recovery AI
9000  protected template
```

и другие объекты, для которых документация задаёт исключение.

---

# 7. Proxmox identities — принято

Имена отражают **актора/роль**, а не название инструмента.

## Host deployer

```text
user:  deployer@pve
token: deployer@pve!host-deploy
```

Актор — PVE-side `deploy-guest`.

Назначение:

- deterministic deployment по `guest.yaml`;
- create/clone VM/LXC;
- поддерживаемая конфигурация CPU/RAM/disk/network/Cloud-Init;
- помещение обычных объектов в `managed`;
- lifecycle, необходимый для deploy/verification;
- работа без `301` и без AI.

## AI actor

```text
user:  ai-agent@pve
token: ai-agent@pve!proximo
```

Актор — AI agent в `301-ai-control`.

Инструмент доступа — Proximo MCP.

Назначение:

- runtime lifecycle разрешённых guests;
- snapshots/backups, если разрешены ACL policy;
- diagnostics/status;
- дальнейшее управление из AI control plane.

Naming:

```text
ai-agent@pve
→ кто действует

!proximo
→ через какой credential/tool path действует
```

Если Proximo заменится, `ai-agent@pve` остаётся корректным; меняется только client/token при необходимости.

## Почему две identity

Разделение принято, потому что:

- host recovery/deploy не зависит от AI credential;
- понятнее audit trail;
- credentials можно независимо отозвать/ротировать;
- права host deployer и runtime AI не обязаны совпадать.

Не использовать ради удобства:

```text
root@pam API token
PVEAdmin на /
```

Точная матрица ACL должна соответствовать отдельной принятой политике проекта.

---

# 8. Хранение secrets на PVE — принято

Для простоты и удобного recovery **все infrastructure secrets, создаваемые bootstrap, постоянно хранятся на самом PVE**.

Не делаем специальную схему, при которой secret `ai-agent@pve!proximo` удаляется с PVE после передачи в `301`.

Целевой каталог:

```text
/etc/proxmox-deployer/secrets/
```

Пример:

```text
/etc/proxmox-deployer/
├── secrets/
│   ├── host-deploy.token
│   └── ai-agent-proximo.token
└── ssh/
    ├── github_proxmox_ed25519
    └── github_proxmox_ed25519.pub
```

Здесь:

```text
host-deploy.token
→ secret deployer@pve!host-deploy

ai-agent-proximo.token
→ secret ai-agent@pve!proximo

github_proxmox_ed25519
→ private read-only GitHub Deploy Key PVE
```

Правила:

- каталог secrets доступен только root и явно нужному runtime;
- token files — `0600`;
- private SSH key — `0600`;
- перед созданием secret files использовать безопасный `umask 077`;
- secrets не выводить повторно в обычные logs;
- secrets не сохранять в `state.json`;
- secrets никогда не помещать в Git;
- bootstrap повторно использует существующий token secret, если он есть и соответствующий token существует;
- потерянный secret требует явной ротации token, а не молчаливого создания нового.

`pvedeploy` должен иметь доступ только к тем secrets, которые нужны host-side deployer. AI token может храниться root-only и передаваться в `301` bootstrap tooling по необходимости.

Преимущество выбранной схемы: PVE остаётся центральной recovery-точкой. При пересоздании `301` существующий `ai-agent@pve!proximo` можно передать новой VM без обязательной ротации.

---

# 9. Linux user `pvedeploy` — принято

`root` нужен для `init-pve.sh` и реальных host-level изменений.

Штатный Git/deploy runtime выполняет отдельный технический Linux user:

```text
pvedeploy
```

Его задачи:

- хранить/использовать PVE read-only GitHub SSH identity;
- владеть private repo checkout;
- запускать manifest parser/deployer;
- использовать credential `deployer@pve!host-deploy`;
- вести runtime/log/state deployer.

Каталоги:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/log/proxmox-deployer/
```

Не выдавать:

```text
NOPASSWD: ALL
```

Нормальный deploy path должен работать через Proxmox API token, а не через полный root/sudo.

---

# 10. PVE GitHub identity — принято

PVE получает собственную пару, независимую от `301`:

```text
/etc/proxmox-deployer/ssh/github_proxmox_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_ed25519.pub
```

Public key оператор один раз регистрирует:

```text
GitHub
→ zsergeyru/proxmox
→ Settings
→ Deploy keys
→ Allow write access = OFF
```

PVE key:

- только read-only;
- только для одного repo;
- не переиспользуется в `301`;
- не является personal credential.

---

# 11. Read-only Git checkout — принято

Каталог:

```text
/var/lib/proxmox-deployer/repo
```

Владелец:

```text
pvedeploy:pvedeploy
```

PVE никогда не делает commit/push из этого checkout.

Первая версия использует shallow checkout (`depth=1`): полное текущее дерево репозитория с минимальной историей Git. Sparse checkout пока не нужен.

Перед deploy:

```text
fetch origin/main
→ определить commit SHA
→ привести read-only checkout к выбранному revision
→ построить PLAN
→ APPLY выполнять по тому же SHA
```

Manifest не должен меняться между PLAN и APPLY.

Deployer показывает revision:

```text
Repository revision: abcdef123456
```

В дальнейшем желательно поддержать:

```bash
deploy-guest 311 --ref <commit> --apply
```

---

# 12. Snapshot host configuration перед изменениями — принято

Это **не VM backup и не полноценный disaster-recovery backup PVE**.

Задача — сохранить конфигурацию, которую bootstrap собирается изменить, чтобы можно было сравнить состояние и вручную откатить ошибочную миграцию.

Каталог:

```text
/var/backups/proxmox-bootstrap/YYYYMMDD-HHMMSS/
```

Сохранять релевантные конфиги и диагностическое состояние, например:

```text
/etc/network/interfaces
/etc/hosts
/etc/hostname
/etc/resolv.conf
APT source files
/etc/pve/storage.cfg
/etc/pve/user.cfg
/etc/pve/datacenter.cfg

pveversion -v
storage config/status
users
roles
ACL
pools
network state
```

Не архивировать бездумно весь `/etc/pve`: это `pmxcfs`, а не обычный каталог.

Этот snapshot не заменяет внешний backup конфигурации хоста.

Infrastructure secrets из `/etc/proxmox-deployer/` должны входить в отдельный защищённый backup PVE, поскольку они нужны для полноценного восстановления bootstrap/deploy identities.

---

# 13. Bootstrap state — принято

Файл:

```text
/var/lib/proxmox-bootstrap/state.json
```

Назначение:

```text
resume + idempotency + понятный progress
```

State может помнить:

- создан ли GitHub key;
- GitHub authorization status;
- bootstrap schema/version;
- последний использованный repo commit;
- ожидаемую template version;
- какие фазы успешно завершались;
- созданы ли PVE identities/tokens.

Но **самих secrets в state нет**.

`state.json` не является source of truth. Каждый повторный запуск перепроверяет реальное состояние PVE, файлов secrets, Git и template.

---

# 14. Bootstrap version — принято

У bootstrap есть собственная версия/schema, например:

```text
PVE-Bootstrap-Version: 1
```

Она нужна, чтобы:

- понимать уже применённую версию;
- выполнять миграции `v1 → v2`;
- не повторять одноразовые/destructive стадии;
- сообщать operator о необходимости migration.

---

# 15. Итоговый health report — принято

После каждого запуска выводится понятный итоговый статус, например:

```text
PVE INIT STATUS

[OK] Proxmox detected
[OK] KVM
[OK] pve-no-subscription repository
[OK] DNS
[OK] time sync
[OK] local
[OK] local-lvm
[OK] snippets
[OK] pvedeploy Linux user
[OK] GitHub Deploy Key
[OK] GitHub read-only access
[OK] private repo checkout
[OK] managed pool
[OK] deployer@pve!host-deploy
[OK] ai-agent@pve!proximo
[OK] secrets storage
[OK] template 9000
[OK] deploy-guest

Repository revision: abcdef123456
Bootstrap version: 1

READY
```

Если требуется ручная регистрация GitHub key:

```text
WAITING FOR GITHUB AUTHORIZATION
```

а не ложный `READY`.

---

# 16. Создание template 9000 — принято

После получения private repo публичный `init-pve.sh` не содержит копию большого template builder.

Целевая модель:

```text
PUBLIC zsergeyru/proxmox-bootstrap
└── init-pve.sh

PRIVATE zsergeyru/proxmox
├── scripts/pve/create-template.sh
├── scripts/pve/deploy-guest...
├── guests/*/guest.yaml
└── остальной desired state
```

Flow:

```text
private Git access появился
→ init-pve вызывает канонический private scripts/pve/create-template.sh
→ проверяет результат
→ template 9000 tpl-debian13 готов
```

Если template уже существует и соответствует принятой версии/policy, он не пересоздаётся без отдельного rebuild режима.

---

# 17. Целевой flow `init-pve.sh`

## Первый запуск

```text
root запускает public init-pve.sh
→ preflight
→ snapshot изменяемой host configuration
→ настроить pve-no-subscription
→ установить минимальные packages
→ проверить DNS/time/storage
→ создать pvedeploy
→ создать PVE read-only GitHub key
→ показать .pub
→ WAITING FOR GITHUB AUTHORIZATION
```

Оператор один раз регистрирует `.pub` как read-only Deploy Key.

## Повторный запуск

```text
init-pve.sh
→ проверить GitHub SSH auth
→ shallow clone/fetch private zsergeyru/proxmox
→ зафиксировать repo commit SHA
→ создать managed pool
→ создать/проверить deployer@pve!host-deploy
→ создать/проверить ai-agent@pve!proximo
→ сохранить оба token secrets на PVE
→ запустить private create-template.sh
→ установить private deploy-guest tooling
→ фактически проверить каждую фазу
→ записать state/version
→ health report
→ READY
```

При bootstrap `301` существующий secret `ai-agent@pve!proximo` передаётся из защищённого PVE storage в AI control plane. Копия на PVE остаётся.

Все стадии должны быть идемпотентными.

---

# 18. Что не входит в `init-pve.sh`

Не устанавливать и не настраивать здесь:

- Docker;
- Hermes/другой AI agent;
- SmartDNS/VPN/PBR;
- Ansible/Semaphore;
- monitoring stack;
- приложения конкретных VM;
- guest `rootfs`;
- model/provider credentials.

Это private desired state соответствующих guests/services.

---

# Security summary

```text
root
→ zero-day host initialization и host-level операции

pvedeploy (Linux)
→ read-only Git
→ host deploy runtime

PVE deployer@pve!host-deploy
→ host-side guest deployment role

PVE ai-agent@pve!proximo
→ AI runtime role через Proximo

/etc/proxmox-deployer/secrets/
→ постоянное локальное хранилище infrastructure token secrets

managed pool
→ обычная runtime write-zone
```

Private keys, token secrets и passwords не хранятся в Git или `state.json`.

PVE GitHub Deploy Key — read-only.

Все bootstrap infrastructure secrets сохраняются на PVE и должны входить в защищённый backup конфигурации хоста.

`100`, `301`, `320`, `9000` и другие явно защищённые объекты не должны автоматически попадать под обычную self-managed write policy.
