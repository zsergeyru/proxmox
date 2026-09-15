# Файловая структура PVE bootstrap/deployer

Этот документ фиксирует размещение файлов после разделения bootstrap на публичную Stage 0 и приватную Stage 1.

Парный документ: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Stage 0
→ только временный zero-day handoff
→ /var/lib/proxmox-bootstrap после успеха удаляется целиком

Stage 1
→ создаёт постоянную инфраструктурную файловую структуру
→ постоянный state хранится в /var/lib/proxmox-deployer/state
```

---

# 1. Public Stage 0

Публичный репозиторий:

```text
zsergeyru/proxmox-bootstrap
└── init-pve.sh
```

Публичная стадия не создаёт PVE roles/tokens/pools/template.

До успешной передачи управления private bootstrap она использует временную область:

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
→ временный read-only GitHub Deploy Key Stage 0

github_proxmox_repo_ed25519.pub
→ public часть, которую человек добавляет в GitHub Deploy keys

known_hosts + ssh_config
→ безопасный SSH transport к GitHub

private-repo/
→ временный shallow clone zsergeyru/proxmox глубиной 1 commit
→ нужен только для запуска Stage 1
```

Если private key уже существует, `.pub` каждый запуск восстанавливается из него. Private key является source of truth для временной пары ключей.

Если Deploy Key ещё не авторизован, public loader выводит `.pub`, инструкцию, ждёт Enter через `/dev/tty` и один раз повторяет проверку доступа.

После успешной private Stage 1:

```text
canonical Deploy Key уже сохранён в /etc/proxmox-deployer/ssh
PVE guest SSH identity уже создана в /etc/proxmox-deployer/ssh
canonical private checkout уже создан в /var/lib/proxmox-deployer/repo
bootstrap state уже находится в /var/lib/proxmox-deployer/state
```

После этого public loader:

```text
→ удаляет /var/lib/proxmox-bootstrap целиком
→ только после успешного удаления публикует stage0-complete marker
```

Постоянный marker:

```text
/var/lib/proxmox-deployer/state/stage0-complete
```

То есть после успешного Stage 0 каталога `/var/lib/proxmox-bootstrap` быть не должно.

---

# 2. Private Stage 1

Канонический private entrypoint:

```text
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
```

Он принимает временный Deploy Key от Stage 0, автономно конфигурирует сам PVE и формирует постоянную структуру.

Stage 1 создаёт отдельную SSH identity для host-side доступа PVE к новым гостям. Эта identity не является GitHub Deploy Key и не является будущей Ansible identity 311.

---

# 3. Постоянная конфигурация deployer

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
→ отдельный private key PVE host-side tooling для SSH-доступа PVE к управляемым VM/LXC

ssh/pve_guest_ed25519.pub
→ public key, который deploy-guest передаёт создаваемому гостю штатным для его типа способом

ssh/config + known_hosts
→ SSH transport для private Git checkout

secrets/host-deploy.token
→ credential deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ recovery/source credential ai-agent@pve!infra
```

`pve_guest_ed25519` создаётся Private Stage 1 один раз и автоматически не ротируется. При повторном bootstrap public часть восстанавливается из private key. Потеря private key при сохранившемся `.pub` считается recovery-ситуацией и не должна приводить к молчаливой генерации новой identity.

---

# 4. Права на `/etc/proxmox-deployer`

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

Общие правила:

- secrets создаются с безопасным `umask`;
- private keys и token secrets не попадают в Git;
- secrets не выводятся в обычные logs;
- потеря local token secret при существующем PVE token требует явной recovery/rotation operation;
- потеря `pve_guest_ed25519` не вызывает автоматическую ротацию: новый ключ создаётся только осознанно;
- `pvedeploy` не получает `ai-agent-infra.token`.

---

# 5. Private Git checkout

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
├── ansible/
└── scripts/
    └── pve/
        ├── bootstrap/
        │   └── init-pve.sh
        ├── create-template.sh
        └── deploy-guest.py
```

`deploy-guest.py` добавляется после реализации; до этого private bootstrap корректно сообщает состояние `partial`.

Этот checkout принадлежит host-side infrastructure runtime и обновляется read-only из GitHub. Полная Git history не требуется: bootstrap использует shallow clone/fetch глубиной 1 commit.

---

# 6. Mutable runtime

```text
/var/lib/proxmox-deployer/
├── repo/
├── state/
└── cache/
```

Назначение:

```text
repo/
→ private source of truth checkout

state/
→ deployer/bootstrap runtime state, last applied revision и stage0 marker

cache/
→ безопасно пересоздаваемый cache
```

Не хранить здесь API token secrets или private SSH keys.

---

# 7. Bootstrap state

Постоянный bootstrap state входит в общий runtime deployer:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
└── stage0-complete
```

Назначение:

```text
state.json
→ текущее состояние private bootstrap

version
→ версия private bootstrap schema

last-run.json
→ timestamp/result/private revision без secrets

last-revision
→ revision канонического private checkout

stage0-complete
→ public zero-day handoff уже успешно завершён
```

State не является source of truth: повторный private bootstrap перепроверяет реальное состояние PVE и наличие permanent credentials.

`/var/lib/proxmox-bootstrap` не является постоянным state-каталогом и после успешного Stage 0 отсутствует.

---

# 8. Логи

```text
/var/log/proxmox-bootstrap/
└── init-pve.log

/var/log/proxmox-deployer/
├── deploy-guest.log
└── audit/
```

Правила:

- не писать token secrets/private keys;
- логировать итоговые стадии и warnings;
- логировать private repo revision;
- не делать полный dump environment с credentials.

Каталог логов `proxmox-bootstrap` является постоянным журналом и не относится к временной `/var/lib/proxmox-bootstrap`.

---

# 9. Bootstrap snapshots

```text
/var/backups/proxmox-bootstrap/
└── YYYYMMDD-HHMMSS/
```

Это снимок host-конфигурации перед изменяющими bootstrap-операциями, а не backup VM.

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

---

# 10. Backup infrastructure secrets

Для защищённой копии:

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

`pve_guest_ed25519` особенно важен для recovery: он может уже быть установлен в `authorized_keys` существующих гостей. Его потеря не должна компенсироваться автоматической незаметной генерацией новой пары.

Локальный backup на том же SSD не считается полноценным disaster-recovery backup.

Backup `301-ai-control` также чувствителен, но не заменяет host-side infrastructure backup.

---

# 11. Стабильные команды

```text
/usr/local/sbin/
├── deploy-guest
└── pve-bootstrap-status
```

`pve-bootstrap-status` устанавливается private Stage 1 и читает:

```text
/var/lib/proxmox-deployer/state/state.json
```

`deploy-guest` после реализации является небольшой wrapper-командой к source из private checkout:

```text
/usr/local/sbin/deploy-guest
→ /var/lib/proxmox-deployer/repo/scripts/pve/deploy-guest.py
```

Канонический source code не копируется в `/usr/local/sbin`.

---

# 12. Локальная копия публичного loader

Пользователь при желании может сохранить public zero-day script как:

```text
/root/init-pve.sh
```

После успешного `stage0-complete` он больше не является рабочим runtime/deployer entrypoint.

Повторные инфраструктурные операции выполняются private bootstrap/tooling.

---

# 13. Итоговое дерево

После успешного Stage 0 временного `/var/lib/proxmox-bootstrap` в итоговом дереве нет:

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
│   │       │   └── stage0-complete
│   │       └── cache/
│   ├── log/
│   │   ├── proxmox-bootstrap/
│   │   └── proxmox-deployer/
│   └── backups/
│       ├── proxmox-bootstrap/
│       └── proxmox-secrets/
│
└── usr/local/sbin/
    ├── deploy-guest
    └── pve-bootstrap-status
```

Главный принцип:

> Public Stage 0 использует `/var/lib/proxmox-bootstrap` только как закрытую временную рабочую область. После успешного handoff она удаляется целиком. Постоянные credentials, включая отдельную PVE guest SSH identity, private checkout, bootstrap state, PVE configuration и runtime принадлежат private Stage 1 и размещаются в канонической структуре deployer.
