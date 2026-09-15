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
Public Stage 0:  STAGE0_VERSION=2
Private Stage 1: BOOTSTRAP_VERSION=7
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
→ проверить read-only доступ к zsergeyru/proxmox
```

Если Deploy Key ещё не авторизован:

```text
вывести public key человеку
→ показать GitHub Settings → Deploy keys
→ напомнить Allow write access = OFF
→ ждать Enter через /dev/tty
→ повторно проверить доступность GitHub
→ один раз повторить Git access check
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
→ canonical Deploy Key уже сохранён private bootstrap
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
storage/snippets
pvedeploy
canonical Git checkout
managed pool
PVE roles
PVE users/API tokens/ACL
secrets
protected template 9000
stable local commands
status/reporting
```

После этого публичный repository больше не нужен для обычной эксплуатации.

---

# 5. Целевое состояние

После успешной Stage 1 PVE имеет:

```text
PVE
├── pve-no-subscription repository
├── bootstrap/admin toolset
├── Linux user pvedeploy
├── canonical read-only GitHub Deploy Key
├── canonical read-only checkout zsergeyru/proxmox
├── resource pool managed
├── deployer@pve!host-deploy
├── ai-agent@pve!infra
├── защищённые локальные копии API token secrets
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
- `pveversion`, `qm`, `pct`, `pvesh`, `pveum`, `pvesm`;
- Debian `trixie` для текущего PVE baseline;
- KVM;
- `vmbr0`;
- `local` и `local-lvm`;
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
- storage/network diagnostics.

Это bootstrap snapshot, а не VM backup и не полный disaster recovery PVE.

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

Enterprise repository отключается, если подписки нет.

Обычная Stage 1 делает `apt update` и ставит нужные пакеты, но не обязана выполнять полный upgrade.

Полное обновление запускается явно:

```bash
init-pve.sh --update-system
```

Public Stage 0 этот параметр только передаёт private Stage 1.

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

# 10. Storage

Базовые storage:

```text
local
local-lvm
```

Private Stage 1 проверяет `snippets` в `local` и при необходимости добавляет этот content type, не удаляя уже разрешённые типы.

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
- затем этим token+secret выполняется реальная локальная авторизация в Proxmox API.

---

# 15. GitHub Deploy Key после handoff

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

После успешного завершения private Stage 1 public Stage 0 удаляет **всю** временную `/var/lib/proxmox-bootstrap`, а не только отдельные key/checkout files.

---

# 16. Канонический private checkout

```text
/var/lib/proxmox-deployer/repo
```

Это read-only checkout `zsergeyru/proxmox` для host-side infrastructure tooling.

Bootstrap использует shallow clone/fetch глубиной 1 commit: полная Git history на PVE для runtime не требуется.

Перед повторным `fetch/reset` проверяется, что `origin` совпадает с каноническим private repository URL.

---

# 17. Template

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

---

# 18. deploy-guest

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

---

# 19. Повторный запуск

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

При начале private запуска `state.json` получает `running`; при ошибке — `failed`; после нормального завершения — итоговый `ready`, `ready-with-warnings` или `partial`.

---

# 20. Итоговый flow

```text
чистый PVE
    │
    ▼
PUBLIC Stage 0
    │
    ├── lock + GitHub connectivity
    ├── временный read-only Deploy Key
    ├── при необходимости: показать key → ждать Enter → retry
    └── shallow private clone depth=1
             │
             ▼
PRIVATE Stage 1
    │
    ├── PVE configuration
    ├── pvedeploy
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

> Public repo обеспечивает только безопасный переход к private source of truth. Временная zero-day область исчезает после успешного handoff; постоянные credentials, checkout, state и вся инфраструктурная логика принадлежат private Stage 1.
