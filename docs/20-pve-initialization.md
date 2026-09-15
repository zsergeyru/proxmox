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
→ обеспечить минимальный git/ssh toolset
→ создать временный read-only GitHub Deploy Key
→ показать public key человеку
→ после добавления ключа проверить доступ к zsergeyru/proxmox
→ временно clone private repo
→ вызвать private scripts/pve/bootstrap/init-pve.sh
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
- deploy-guest implementation;
- внутренней файловой структуре deployer.

Единственная неизбежная инфраструктурная информация в public repo — адрес приватного GitHub repository и путь private bootstrap entrypoint.

## Временные файлы Stage 0

До успешного handoff допускается использовать:

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
→ временный Stage 0 key удаляется
→ временный checkout удаляется
→ остаётся stage0-complete marker
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

После добавления ключа тот же public `init-pve.sh` запускается повторно.

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
├── bootstrap state/version/log
└── health report
```

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

Protected объекты по умолчанию не помещаются в обычную AI write-zone, в частности:

```text
100   production HAOS
301   AI control plane
320   bootstrap/legacy AI control
9000  protected template
```

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

Правило идемпотентности:

```text
роль отсутствует
→ создать

роль существует и совпадает
→ переиспользовать

роль существует, но отличается
→ вывести warning + missing/extra
→ НЕ менять автоматически
→ НЕ останавливать bootstrap
```

Аналогично дополнительные старые ACL не удаляются автоматически.

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
- Proxmox token secret сохраняется сразу после создания;
- существующий token + существующий secret переиспользуются;
- если token существует, а локальный secret утрачен, bootstrap останавливается и требует явную rotation/recovery operation;
- bootstrap не пытается молча пересоздать такой credential.

---

# 15. GitHub Deploy Key после handoff

Stage 0 создаёт временную zero-day копию ключа.

Private Stage 1 принимает её и сохраняет канонически:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519.pub
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
```

После успешного завершения private Stage 1 public Stage 0 удаляет временную копию key и временный checkout.

---

# 16. Канонический private checkout

```text
/var/lib/proxmox-deployer/repo
```

Это read-only checkout `zsergeyru/proxmox` для host-side infrastructure tooling.

При повторной синхронизации используется текущий `main` согласно принятой versioning policy проекта.

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

Template остаётся protected и вне обычной write-zone `managed`.

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
/var/lib/proxmox-bootstrap/stage0-complete
```

Повторный public запуск больше не пытается строить инфраструктуру.

## Private Stage 1

Private bootstrap рассчитан на повторный запуск и перепроверяет фактическое состояние PVE.

Он не должен молча выполнять destructive overwrite существующих VM/LXC, roles, credentials или ACL.

---

# 20. Итоговый flow

```text
чистый PVE
    │
    ▼
PUBLIC Stage 0
    │
    ├── minimal Git/SSH
    ├── read-only Deploy Key
    └── private clone
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
    └── local tooling
             │
             ▼
        рабочий PVE
```

Главный принцип:

> Public repo обеспечивает только безопасный переход к private source of truth. Вся инфраструктурная логика и сведения о внутреннем устройстве PVE находятся в `zsergeyru/proxmox`.
