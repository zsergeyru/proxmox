# Guest bootstrap и provisioning

**Type:** Policy / Design  
**Status:** Active  
**Source of truth:** Yes — для границы `deploy-guest` ↔ bootstrap ↔ Ansible provisioning и initial management readiness.

Общая SSH identity policy находится в [`23-security.md`](23-security.md). Формат guest manifest — в [`30-guest-manifest.md`](30-guest-manifest.md).

## 1. Принятая модель

```text
deploy-guest.py
→ PVE lifecycle
→ initial management access
→ verify root SSH
→ optional ограниченный bootstrap
→ handoff к Ansible
→ штатный provisioning ОС и приложений
```

Поля `bootstrap` и `provisioning` пока не входят в действующие guest schema и должны появиться одновременно с их реализацией в resolver, validator и deployer.

Ключевое правило:

> Initial SSH access является частью deploy readiness, а не bootstrap capability.

Guest должен быть доступен как `root` по management key до запуска `base`, `git`, `docker` или другой optional capability.

## 2. Ответственность `deploy-guest.py`

Deployer отвечает прежде всего за Proxmox-side desired state и management readiness:

```text
создать/найти VM или LXC
→ CPU / RAM / disk / network
→ pool / protection / boot
→ установить initial SSH public keys штатным механизмом типа guest
→ start
→ дождаться root SSH
→ verify management access
```

После этого он может выполнить только небольшой whitelist bootstrap capabilities.

Deployer не должен становиться вторым configuration-management framework.

## 3. Management user

Для управляемых Debian VM/LXC используется:

```text
root:22
```

Root password locked, password SSH disabled, разрешён только public-key authentication.

Generic user `ops` не является частью guest contract.

Полная credential policy и список независимых identities описаны в [`23-security.md`](23-security.md).

## 4. Host-side PVE SSH identity

Host-side deployment использует постоянный keypair:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

`deploy-guest`:

- не создаёт keypair;
- не ротирует его;
- использует public key при initial guest creation;
- использует private key для SSH verification/bootstrap после start.

Credential создаёт и восстанавливает PVE bootstrap/configuration layer. Потеря private key — recovery case, а не повод для silent rotation.

## 5. Initial access: VM

Текущий template `9000` предоставляет универсальный Debian baseline без baked-in management keys.

Новый Full Clone до первого start получает необходимые public keys через Cloud-Init:

```text
Full Clone from 9000
→ hostname / CPU / RAM / disk / network
→ ciuser=root
→ sshkeys=<initial public keys>
→ qm cloudinit update
→ start
→ verify QGA/Cloud-Init при необходимости
→ verify root SSH
```

Private keys через Cloud-Init не передаются.

Точный contract template находится в [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## 6. Initial access: LXC

Для Debian LXC:

```text
resolve Debian appliance
→ собрать initial public key set
→ create LXC с ssh-public-keys
→ start
→ verify root SSH
```

Proxmox устанавливает переданные public keys для `root` при создании контейнера. Промежуточный bootstrap-user, `ops` и настройка sudo для получения первого доступа не нужны.

`base` capability в этом flow не участвует.

## 7. Initial public key set

Минимально host-side deploy должен уметь создать guest с:

```text
pve_guest_ed25519.pub
```

Дополнительно initial set может включать зарегистрированные public infrastructure credentials:

```text
ai_control_ed25519.pub
ansible/provisioning public key
other explicitly requested public keys
```

Public keys можно передавать между управляющими контурами; private halves между ними не копируются.

Если AI public key ещё не зарегистрирован в host-side deploy runtime, это не должно блокировать базовое создание guest с PVE management key.

Pool `managed` не является источником guest SSH policy: membership в pool само по себе не добавляет и не удаляет public keys.

## 8. Guests, создаваемые AI

Если guest создаёт AI через Proximo, он может передать свой public key через поддерживаемые VM/LXC create options.

Когда host-side public key доступен AI control plane как зарегистрированный **public** infrastructure credential, его также следует включать, чтобы сохранить независимый PVE management channel.

Private host-side key на AI Control не копируется.

## 9. Bootstrap capabilities

Планируемый manifest contract:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Начальный whitelist:

- `base` — минимальные OS prerequisites после уже работающего root SSH;
- `git` — Git там, где он нужен для bootstrap/handoff;
- `docker` — project Docker runtime и Compose plugin;
- `ansible_controller` — containerized Ansible controller / Execution Environment.

Отсутствующая capability считается не запрошенной. Capability должна быть идемпотентной.

`base: false` или отсутствие `base` не должно влиять на возможность management SSH.

## 10. Что не относится к bootstrap

Не вводить generic package interface вроде:

```yaml
bootstrap:
  packages:
    - nginx
    - postgresql
    - redis
```

PostgreSQL, MQTT, Grafana, Gitea, Semaphore, Jenkins и другие application services устанавливаются provisioning-слоем.

## 11. Provisioning

Для штатной конфигурации ОС и приложений используется Ansible.

Планируемая декларация высокого уровня:

```yaml
provisioning:
  capabilities:
    base: true
    docker: true
    git: true
    monitoring: true
```

Resolver строит effective desired state, а provisioning layer передаёт capabilities в Ansible playbook как variables.

Знания о конкретной установке и настройке ПО живут в Ansible roles, а не в `guest.yaml` и не в deployer.

Стандарт размещения application/config/data внутри Linux guest находится в [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md).

## 12. Специальный bootstrap `311-dev-services`

`311-dev-services` — штатный Ansible/control node.

Первоначальный flow:

```text
deploy-guest 311 --apply
→ create LXC 311 с root SSH public keys
→ start
→ verify root SSH
→ optional base
→ Git
→ Docker
→ Ansible controller
→ handoff к Ansible
```

Целевая структура:

```text
311-dev-services
├── Debian 13
├── root SSH key-only
├── Git
├── Docker Engine + Compose plugin
├── checkout проекта proxmox
└── containerized Ansible Execution Environment
```

Semaphore, Git service, CI и другие workloads 311 не относятся к bootstrap.

## 13. Обычные Linux guests

Для обычных VM/LXC deployer гарантирует PVE desired state и management access, после чего штатная конфигурация выполняется с 311:

```text
311 / Ansible EE
      │
      ├─ SSH root → 109
      ├─ SSH root → 211
      ├─ SSH root → 301
      ├─ SSH root → 311
      ├─ SSH root → 321
      ├─ SSH root → 331
      └─ SSH root → 401
```

На target-гостях Ansible устанавливать не требуется. Нужны SSH и prerequisites используемых modules.

Host-side, AI и Ansible credentials не подменяют друг друга.

## 14. Реализация bootstrap interface

Предпочтительная структура:

```text
scripts/
├── pve/
│   └── deploy-guest.py
└── bootstrap/
    ├── base.py
    ├── git.py
    ├── docker.py
    └── ansible_controller.py
```

Initial SSH injection/verification находится в ядре `deploy-guest.py`, а не в `bootstrap/base.py`.

`scripts/guest_config.py` остаётся единым resolver source → effective state для validator и deployer.

## 15. Security constraints

- secrets/private keys/passwords/tokens не хранятся в `guest.yaml`;
- deployer не генерирует и не ротирует infrastructure SSH credentials;
- bootstrap handlers не печатают secrets;
- GitHub key не используется для guest SSH;
- AI и Ansible имеют собственные independent keypairs;
- Ansible EE получает только необходимые credentials;
- application не получает bootstrap capability только ради удобства установки.

Общие правила security и backup private keys — в [`23-security.md`](23-security.md).

## 16. Итоговая цепочка

```text
PVE bootstrap/configuration
   └─ pve_guest_ed25519

            ↓

guest.yaml
   ↓
scripts/guest_config.py
   ↓
effective desired state
   ↓
deploy-guest.py
   ├─ PVE PLAN/APPLY
   ├─ inject root SSH public keys
   ├─ verify root SSH
   └─ optional bootstrap capabilities
             ↓
       Ansible EE на 311
             ↓
   provisioning playbooks/roles
             ↓
     application workloads
```

Главное правило: **management SSH — часть deploy readiness. Bootstrap начинается только после появления проверенного management channel и заканчивается там, где начинается repeatable provisioning.**
