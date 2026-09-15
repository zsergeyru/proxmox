# Инициализация нового Proxmox VE host

## Статус решения

Архитектура PVE bootstrap разделена на **две стадии**.

```text
Stage 0 — PUBLIC
zsergeyru/proxmox-bootstrap/init-pve.sh

Stage 1 — PRIVATE
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
```

Публичная Stage 0 является только zero-day загрузчиком. Она не содержит внутреннюю модель инфраструктуры и после получения read-only доступа передаёт управление private source of truth.

Полная конфигурация PVE выполняется только Stage 1 из приватного `zsergeyru/proxmox`.

Текущие версии:

```text
Public Stage 0:  STAGE0_VERSION=3
Private Stage 1: BOOTSTRAP_VERSION=11
```

---

# 1. Исходное состояние

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
- PVE guest SSH identity;
- template `9000`;
- проектных PVE users/roles/tokens/pools.

---

# 2. Stage 0 — публичный zero-day handoff

Активная публичная точка входа:

```text
zsergeyru/proxmox-bootstrap/init-pve.sh
```

Её задача намеренно минимальна:

```text
проверить root + наличие Proxmox VE
→ получить exclusive Stage 0 lock
→ обеспечить минимальный git/ssh toolset
→ проверить DNS/HTTPS-доступность GitHub
→ создать или переиспользовать временный read-only GitHub Deploy Key
→ проверить read-only доступ к zsergeyru/proxmox и ветке main
```

Если Deploy Key ещё не авторизован:

```text
вывести public key человеку
→ показать GitHub Settings → Deploy keys
→ напомнить Allow write access = OFF
→ ждать Enter через /dev/tty
→ повторно проверить доступность GitHub
→ один раз повторить Git access check ветки main
```

Если после Enter доступ по-прежнему отсутствует, Stage 0 завершается с явной ошибкой. При следующем запуске существующий временный private key переиспользуется, а public часть восстанавливается из него.

После успешной авторизации:

```text
проверить origin существующего временного checkout, если он есть
→ shallow clone/fetch private repo с depth=1
→ вызвать private scripts/pve/bootstrap/init-pve.sh
→ после успешного handoff удалить временную Stage 0 область целиком
→ создать постоянный stage0-complete marker
```

Stage 0 не должна содержать сведения и код, относящиеся к:

- `managed` и другим resource pools;
- PVE roles/ACL;
- `deployer@pve!host-deploy`;
- `ai-agent@pve!infra`;
- API token secrets;
- PVE guest SSH identity;
- VMID plan;
- template `9000`;
- Proximo/AI runtime;
- deploy-guest implementation.

Публичная стадия знает только минимум, необходимый для handoff: адрес private repository, private bootstrap entrypoint и путь постоянного marker завершения Stage 0.

## Временные файлы Stage 0

До успешного handoff используется закрытая область `root:root 0700`:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

После успешной private Stage 1:

```text
→ canonical GitHub Deploy Key уже сохранён private bootstrap
→ отдельная PVE guest SSH identity уже создана private bootstrap
→ canonical private checkout уже создан
→ private bootstrap state уже хранится в /var/lib/proxmox-deployer/state
→ /var/lib/proxmox-bootstrap удаляется целиком
→ stage0-complete создаётся только после успешного cleanup
```

Постоянный marker:

```text
/var/lib/proxmox-deployer/state/stage0-complete
```

Stage 0 не является постоянным runtime layer.

---

# 3. Однократная авторизация GitHub

Stage 0 генерирует отдельный SSH Deploy Key непосредственно на PVE.

Ключ добавляется в:

```text
zsergeyru/proxmox
Settings → Deploy keys → Add deploy key
```

Обязательно:

```text
Allow write access: OFF
```

Обычный сценарий больше не требует обязательного второго запуска: после показа public key Stage 0 ждёт Enter, затем повторяет проверку и продолжает тот же запуск.

Если человек нажал Enter, а ключ не был добавлен или SSH-доступ к GitHub не работает, Stage 0 завершается. После исправления можно повторно запустить тот же public `init-pve.sh`: временный private key сохраняется до успешного handoff.

Никакие private keys или token secrets через Git не передаются.

После успешного Stage 0 повторный public запуск с `--update-system` не игнорируется молча: скрипт явно направляет оператора к canonical private init:

```bash
/var/lib/proxmox-deployer/repo/scripts/pve/bootstrap/init-pve.sh --update-system
```

---

# 4. Stage 1 — приватная полная инициализация

Канонический executable:

```text
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
```

Stage 1 принимает read-only Deploy Key от Stage 0, переносит его в постоянную файловую структуру и затем самостоятельно выполняет всю настройку PVE.

Именно private Stage 1 отвечает за:

```text
host preflight
configuration snapshot
APT policy
packages
DNS/time/outbound checks
storage/content types
Debian 13 LXC template
pvedeploy + config.yaml
PVE guest SSH identity
canonical Git checkout
managed pool
PVE roles
PVE users/API tokens/ACL
secrets
protected template 9000
stable local commands
status/reporting
```

Stage 1 автономно настраивает сам PVE и не использует `guest.yaml`, `guests/defaults.yaml` или guest profiles как входные данные для bootstrap хоста.

После этого публичный repository больше не нужен для обычной эксплуатации.

---

# 5. Целевое состояние

После успешной Stage 1 PVE имеет:

```text
PVE
├── pve-no-subscription repository
├── согласованный Ceph no-subscription channel, если Ceph repo был enterprise
├── bootstrap/admin toolset
├── Linux user pvedeploy
├── canonical read-only GitHub Deploy Key
├── PVE guest SSH identity
├── canonical read-only checkout zsergeyru/proxmox
├── resource pool managed
├── deployer@pve!host-deploy
├── ai-agent@pve!infra
├── защищённые локальные копии API token secrets
├── Debian 13 LXC template в local:vztmpl
├── template 9000 tpl-debian13
├── private PVE tooling
├── /var/lib/proxmox-deployer/state bootstrap/runtime state
└── health report
```

При этом:

```text
/var/lib/proxmox-bootstrap
```

после успешного handoff отсутствует.

Host-side deploy должен работать независимо от AI control plane.

---

# 6. Preflight private Stage 1

Перед существенными изменениями проверяются:

- запуск от `root`;
- Proxmox VE **9.x**;
- Debian `trixie`;
- `pveversion`, `qm`, `pct`, `pvesh`, `pveum`, `pvesm`, `pveam`;
- KVM;
- `vmbr0`;
- наличие `local` и `local-lvm`;
- конфликт VMID `9000`;
- DNS/outbound connectivity;
- источник Debian cloud image, если template ещё отсутствует.

VMID `9000`, занятый неизвестной VM или LXC, не перезаписывается.

---

# 7. Snapshot перед изменениями

Private bootstrap сохраняет диагностический snapshot текущей host-конфигурации до значимых изменений.

Целевой каталог:

```text
/var/backups/proxmox-bootstrap/YYYYMMDD-HHMMSS/
```

Сохраняются, где применимо:

- network/hostname/resolver;
- APT source files;
- `storage.cfg`;
- `user.cfg`;
- `datacenter.cfg`;
- PVE version;
- users/roles/ACL/pools;
- storage/network diagnostics;
- список уже загруженных LXC templates;
- исходный `qm config 9000`, если canonical template уже существует.

Это bootstrap snapshot, а не VM backup и не полный disaster recovery PVE. Постоянные private credentials, включая PVE guest private key, должны дополнительно входить во внешнюю backup policy secrets.

---

# 8. APT policy

Полная APT policy является private implementation detail.

Для PVE без subscription используется:

```text
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
```

Основной `pve-enterprise` отключается.

Ceph repository обрабатывается консервативно:

```text
ceph.sources отсутствует
→ ничего не менять

уже download.proxmox.com + no-subscription
→ ничего не менять

enterprise.proxmox.com + Components: enterprise
→ сохранить тот же ceph-* release
→ перевести только channel на download.proxmox.com + no-subscription

legacy ceph.list, multi-stanza или другая нестандартная конфигурация
→ STOP
→ не переписывать автоматически
```

Bootstrap не выбирает другую Ceph major/release самостоятельно.

Обычная Stage 1 делает `apt update` и ставит нужные пакеты, но не обязана выполнять полный upgrade.

Полное обновление запускается явно:

```bash
init-pve.sh --update-system
```

Public Stage 0 этот параметр передаёт private Stage 1 только при первоначальном handoff.

---

# 9. Пакеты

Private Stage 1 обеспечивает runtime:

```text
git
openssh-client
python3
python3-yaml
curl
jq
ca-certificates
```

и небольшой host-admin набор:

```text
mc
htop
tmux
smartmontools
lm-sensors
```

Public Stage 0 устанавливает только минимальные пакеты, необходимые для GitHub handoff.

На PVE не устанавливать без отдельного решения Docker, AI runtimes и application services.

---

# 10. Storage и LXC template

Базовые storage:

```text
local
local-lvm
```

Stage 1 проверяет, что оба storage включены и активны, после чего additive-only policy обеспечивает content types:

```text
local:
  snippets
  vztmpl

local-lvm:
  images
  rootdir
```

Недостающие content types добавляются без удаления существующих.

Для LXC Stage 1 выполняет:

```text
pveam update
→ найти актуальный debian-13-standard_*_amd64 template в section system
→ проверить local:vztmpl
→ при отсутствии скачать через pveam download local <template>
→ повторно проверить наличие
```

Точное имя template не хардкодится, поэтому timestamp/version appliance определяется текущим каталогом `pveam`.

Это позволяет создавать LXC после bootstrap без выдачи AI права `Datastore.AllocateTemplate`: загрузку base template выполняет root/Stage 1 заранее.

---

# 11. Pool `managed`

Создаётся private Stage 1:

```text
managed
```

`managed` — основная organization/security boundary для обычной AI/runtime automation.

```text
guest в managed
→ AI получает предусмотренные guest-level права

guest вне managed
→ права через managed на него не распространяются
```

Существующий состав `managed` bootstrap специально не анализирует и не исправляет: текущее содержимое pool считается административным состоянием Proxmox.

Полная ACL policy находится в [`25-pve-access-control.md`](25-pve-access-control.md).

---

# 12. Две PVE identity

Private Stage 1 создаёт/проверяет две отдельные identity.

## Человек и host-side tooling

```text
user:  deployer@pve
token: deployer@pve!host-deploy
```

Назначение:

```text
человек
локальные PVE tools
deploy-guest
```

Это не AI identity.

## AI / Proximo

```text
user:  ai-agent@pve
token: ai-agent@pve!infra
```

Назначение:

```text
AI control
→ Proximo
→ Proxmox API
```

Ключевое разделение — разные потребители, audit trail и независимая ротация credentials.

Не использовать вместо них `root@pam` API token или `PVEAdmin` на `/`.

---

# 13. Roles и существующая конфигурация

Private Stage 1 использует принятую схему ролей, включая:

```text
AICloneSource
AIManagedGuest
AINetworkUse
AIStorage
AIManagedPool
```

Правило идемпотентности для наших custom roles:

```text
роль отсутствует
→ создать с требуемыми privileges

роль существует и содержит все требуемые privileges
→ переиспользовать

роль существует, но части требуемых privileges нет
→ добавить только недостающие через append
→ повторно проверить

в роли есть дополнительные privileges
→ сохранить их без автоматического удаления
```

Bootstrap может безопасно расширять наши проектные роли, но автоматически не сужает уже существующую конфигурацию.

Если среди дополнительных прав обнаруживаются особенно опасные `Permissions.Modify` или `Sys.Modify`, bootstrap выводит заметное предупреждение, но по принятой additive policy всё равно не удаляет их автоматически.

Аналогично дополнительные существующие ACL не удаляются автоматически.

---

# 14. API token secrets

Канонические файлы:

```text
/etc/proxmox-deployer/secrets/host-deploy.token
/etc/proxmox-deployer/secrets/ai-agent-infra.token
```

Правила:

- secrets никогда не помещаются в Git;
- `host-deploy.token` доступен `root:pvedeploy` с ограниченными правами;
- `ai-agent-infra.token` — только `root`;
- новый Proxmox token создаётся с `privsep=1` и его одноразовый secret сразу сохраняется;
- существующий token + существующий secret переиспользуются;
- существующий `privsep=1` сохраняется;
- существующий `privsep=0` не меняется автоматически: bootstrap выводит явное предупреждение, чтобы не сломать уже работающий доступ;
- если token существует, а локальный secret утрачен, bootstrap останавливается и требует явную rotation/recovery operation;
- bootstrap не пытается молча пересоздать такой credential;
- после ACL setup проверяются effective permissions токена;
- затем этим token+secret выполняется реальная локальная авторизация в Proxmox API;
- временный `.api-check.*` header удаляется штатно, по `INT/TERM`, а stale-файлы после возможного `SIGKILL` очищаются на следующей проверке.

---

# 15. PVE guest SSH identity

Private Stage 1 создаёт отдельную постоянную Ed25519 identity для host-side SSH-доступа PVE к управляемым Linux VM/LXC:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Назначение разделено явно:

```text
GitHub Deploy Key
→ только read-only Git transport к zsergeyru/proxmox

pve_guest_ed25519
→ постоянный SSH-доступ PVE host-side tooling к управляемым гостям

AI Control infrastructure key
→ отдельная identity внутри AI control plane

Ansible/311 identity
→ отдельная identity provisioning-контура
```

`pve_guest_ed25519` создаётся Stage 1, а не `deploy-guest`. Deployer только использует уже подготовленный credential:

```text
public half
→ передаётся гостю штатным для его типа способом до/при первом management-доступе

private half
→ остаётся только на PVE
→ используется pvedeploy/deploy-guest для SSH-доступа и host-side операций
```

Private key никогда не передаётся в VM/LXC и никогда не хранится в Git.

Правила повторного запуска:

```text
private отсутствует + public отсутствует
→ создать новую Ed25519 keypair

private существует
→ не ротировать
→ проверить читаемость и тип Ed25519
→ восстановить .pub из private key
→ восстановить canonical owner/mode

private отсутствует + public существует
→ STOP
→ считать recovery-ситуацией
→ не генерировать новый ключ молча
```

Канонические права:

```text
pve_guest_ed25519      pvedeploy:pvedeploy 0600
pve_guest_ed25519.pub  pvedeploy:pvedeploy 0644
```

Автоматическая ротация запрещена, потому что public half этого ключа может уже находиться в `authorized_keys` существующих гостей. Потеря private half должна обрабатываться отдельной recovery/rotation процедурой.

`state.json` содержит только признак `pve_guest_key_exists`, но не секретный материал.

Путь ключа не дублируется как обязательный параметр `config.yaml`: это каноническая часть host filesystem layout с фиксированным project path.

---

# 16. GitHub Deploy Key после handoff

Stage 0 создаёт временную zero-day копию ключа:

```text
/var/lib/proxmox-bootstrap/github_proxmox_repo_ed25519
```

Private Stage 1 принимает её и сохраняет канонически:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519.pub
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
```

Canonical `.pub` каждый запуск восстанавливается из private key, поэтому потеря или повреждение только public-файла не требует ротации ключа.

Если canonical private key уже существует, а Stage 0 принесла другой ключ, Stage 1 не заменяет постоянный credential автоматически. При отказе canonical credential bootstrap останавливается с явным recovery/rotation сообщением.

GitHub Deploy Key и `pve_guest_ed25519` не переиспользуют друг друга.

После успешного завершения private Stage 1 public Stage 0 удаляет **всю** временную `/var/lib/proxmox-bootstrap`, а не только отдельные key/checkout files.

---

# 17. Канонический private checkout и config.yaml

```text
/var/lib/proxmox-deployer/repo
```

Это read-only checkout `zsergeyru/proxmox` для host-side infrastructure tooling.

Bootstrap использует shallow clone/fetch глубиной 1 commit: полная Git history на PVE для runtime не требуется.

Перед повторным `fetch/reset` проверяется, что `origin` совпадает с каноническим private repository URL.

Host-side bootstrap config:

```text
/etc/proxmox-deployer/config.yaml
```

Если файла нет, Stage 1 создаёт его. Если файл уже существует, он **не перезаписывается**, но обязательные project-owned keys валидируются:

```text
repo
branch
checkout
managed_pool
host_deploy_identity
ai_infra_identity
template_vmid
```

Отсутствующий или конфликтующий обязательный ключ приводит к STOP вместо продолжения с ложной конфигурацией. Дополнительные неизвестные keys разрешены.

PVE guest key не является настраиваемым path в этом файле: его canonical location фиксирован проектом в `/etc/proxmox-deployer/ssh/`.

---

# 18. Template

Канонический builder:

```text
scripts/pve/create-template.sh
```

Целевой template:

```text
VMID: 9000
Name: tpl-debian13
```

Для canonical `tpl-debian13` bootstrap обеспечивает:

```text
template = 1
protection = 1
```

Если `9000` существует, но это не canonical template, bootstrap останавливается и не перезаписывает объект.

Preflight только проверяет identity существующего template. Если `protection=1` отсутствует, изменение выполняется позднее, уже после configuration snapshot.

---

# 19. deploy-guest

`deploy-guest` — private host-side инструмент и удобный интерфейс для человека.

Он не должен находиться в public bootstrap repository.

Целевой source:

```text
scripts/pve/deploy-guest.py
```

Стабильная host-side команда:

```text
/usr/local/sbin/deploy-guest
```

AI не обязан использовать `deploy-guest`: AI работает через Proximo и `ai-agent@pve!infra`.

Stage 1 заранее создаёт `pve_guest_ed25519`; `deploy-guest` не создаёт и не ротирует этот credential. При создании Linux-гостя он передаёт public half гостю, а private half использует для разрешённых host-side SSH-операций PVE.

Старая executable wrapper без существующего source больше не считается готовым `deploy-guest` и не даёт ложный `[ОК]` в итоговом отчёте.

---

# 20. Повторный запуск и state

## Public Stage 0

После успешного handoff создаётся:

```text
/var/lib/proxmox-deployer/state/stage0-complete
```

а временный:

```text
/var/lib/proxmox-bootstrap
```

уже отсутствует.

Повторный public запуск видит marker и больше не пытается выполнять zero-day handoff.

Если первый запуск прервался до успешного handoff, marker отсутствует, временный Deploy Key сохраняется и следующий запуск может продолжить с той же key pair.

## Private Stage 1

Private bootstrap рассчитан на повторный запуск и перепроверяет фактическое состояние PVE.

Постоянный state:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── stage0-complete
```

При начале private запуска `state.json` получает `running`; при штатной shell/API ошибке — `failed`; при `SIGINT`/`SIGTERM` — `interrupted`; после нормального завершения — итоговый `ready`, `ready-with-warnings` или `partial`.

State не является source of truth: повторный Stage 1 перепроверяет PVE, постоянные credentials и ключевые filesystem prerequisites.

---

# 21. Итоговый flow

```text
чистый PVE 9 / Debian trixie
    │
    ▼
PUBLIC Stage 0
    │
    ├── lock + GitHub connectivity
    ├── временный read-only GitHub Deploy Key
    ├── при необходимости: показать key → ждать Enter → retry
    └── shallow private clone depth=1, branch=main
             │
             ▼
PRIVATE Stage 1
    │
    ├── PVE/Ceph repository policy
    ├── storage content types + Debian 13 LXC template
    ├── pvedeploy + validated config.yaml
    ├── PVE guest SSH identity
    ├── canonical Git checkout
    ├── managed + roles + ACL
    ├── API identities/secrets
    ├── template 9000
    ├── permanent bootstrap state
    └── local tooling
             │
             ▼
PUBLIC Stage 0 cleanup
    │
    ├── удалить /var/lib/proxmox-bootstrap целиком
    └── создать permanent stage0-complete marker
             │
             ▼
        рабочий PVE
```

Главный принцип:

> Public repo обеспечивает только безопасный переход к private source of truth. Временная zero-day область исчезает после успешного handoff; постоянные credentials, включая отдельную PVE guest SSH identity, checkout, state и вся инфраструктурная логика принадлежат private Stage 1.