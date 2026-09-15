# Файловая структура PVE bootstrap/deployer

Этот документ фиксирует размещение файлов после разделения bootstrap на публичную Stage 0 и приватную Stage 1.

Парный документ: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Stage 0
→ только временный zero-day handoff

Stage 1
→ создаёт постоянную инфраструктурную файловую структуру
```

---

# 1. Public Stage 0

Публичный репозиторий:

```text
zsergeyru/proxmox-bootstrap
└── init-pve.sh
```

Публичная стадия не знает внутреннюю структуру deployer и не создаёт PVE roles/tokens/pools/template.

До успешной передачи управления private bootstrap она использует временную область:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Назначение:

```text
github_proxmox_repo_ed25519
→ временный read-only GitHub Deploy Key Stage 0

known_hosts + ssh_config
→ безопасный SSH transport к GitHub

private-repo/
→ временный clone zsergeyru/proxmox только для запуска Stage 1
```

После успешной Stage 1 public loader создаёт:

```text
/var/lib/proxmox-bootstrap/stage0-complete
```

и удаляет:

```text
временный private key
его public часть
Stage 0 ssh_config/known_hosts
временный private-repo checkout
```

То есть Stage 0 не оставляет вторую постоянную копию Git credential или private repository.

---

# 2. Private Stage 1

Канонический private entrypoint:

```text
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
```

Он принимает временный Deploy Key от Stage 0 и формирует постоянную структуру.

---

# 3. Постоянная конфигурация deployer

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
→ постоянные параметры host-side deployer

ssh/github_proxmox_repo_ed25519
→ canonical read-only Deploy Key для zsergeyru/proxmox

ssh/config + known_hosts
→ SSH transport для private Git checkout

secrets/host-deploy.token
→ credential deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ recovery/source credential ai-agent@pve!infra
```

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
```

Общие правила:

- secrets создаются с безопасным `umask`;
- private keys и token secrets не попадают в Git;
- secrets не выводятся в обычные logs;
- потеря local token secret при существующем PVE token требует явной recovery/rotation operation;
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

Этот checkout принадлежит host-side infrastructure runtime и обновляется read-only из GitHub.

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
→ deployer runtime state, last applied revision и т.п.

cache/
→ безопасно пересоздаваемый cache
```

Не хранить здесь API token secrets.

---

# 7. Bootstrap state

```text
/var/lib/proxmox-bootstrap/
├── state.json
├── version
├── last-run.json
└── stage0-complete
```

После завершения public handoff временных credentials здесь быть не должно.

Назначение:

```text
state.json
→ текущее состояние private bootstrap

version
→ версия private bootstrap schema

last-run.json
→ timestamp/result/private revision без secrets

stage0-complete
→ public zero-day handoff уже успешно завершён
```

State не является source of truth: повторный private bootstrap перепроверяет реальное состояние PVE.

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
/etc/proxmox-deployer/ssh/config
/etc/proxmox-deployer/ssh/known_hosts
```

Локальный backup на том же SSD не считается полноценным disaster-recovery backup.

Backup `301-ai-control` также чувствителен, но не заменяет host-side infrastructure backup.

---

# 11. Стабильные команды

```text
/usr/local/sbin/
├── deploy-guest
└── pve-bootstrap-status
```

`pve-bootstrap-status` устанавливается private Stage 1.

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
│   │   │   ├── state/
│   │   │   └── cache/
│   │   └── proxmox-bootstrap/
│   │       ├── state.json
│   │       ├── version
│   │       ├── last-run.json
│   │       └── stage0-complete
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

> Public Stage 0 оставляет после себя только факт успешного handoff. Постоянные credentials, private checkout, PVE configuration и runtime принадлежат private Stage 1 и размещаются по канонической структуре проекта.
