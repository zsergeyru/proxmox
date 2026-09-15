# Guest bootstrap и provisioning

## Статус решения

Принята двухуровневая модель настройки Linux-гостей:

```text
deploy-guest.py
→ PVE lifecycle
→ гарантированный management SSH
→ optional ограниченный bootstrap
→ передача управления Ansible
→ штатный provisioning ОС и приложений
```

Поля `bootstrap` и `provisioning` пока не входят в действующие guest schema v5 и должны быть добавлены одновременно с реализацией соответствующей поддержки в `deploy-guest.py` и validator.

Ключевое правило:

> Получение первоначального SSH-доступа не является bootstrap capability. Guest должен быть доступен как `root` по management key до запуска `base`, `git`, `docker` или любой другой optional capability.

## Граница ответственности

`deploy-guest.py` отвечает прежде всего за Proxmox-side desired state и management readiness:

```text
создать/найти VM или LXC
→ CPU / RAM / disk
→ network
→ pool / protection / boot
→ установить initial SSH public keys штатным механизмом типа guest
→ start
→ дождаться root SSH
→ verify management access
```

Только после этого могут выполняться optional bootstrap capabilities.

Deployer не должен становиться вторым configuration-management framework.

## Единый management user

Для управляемых Debian VM/LXC используется:

```text
root:22
```

Root password заблокирован. SSH password authentication отключена. Разрешён только public-key authentication.

Generic user `ops` не является частью guest contract и не должен создаваться `base` или `deploy-guest` ради административного доступа.

## PVE SSH identity

Отдельную постоянную SSH identity для host-side доступа PVE к управляемым VM/LXC создаёт **PVE Private Stage 1**, а не `deploy-guest.py`.

Канонические файлы:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Разделение credentials:

```text
github_proxmox_repo_ed25519
→ только read-only доступ PVE к GitHub repository

pve_guest_ed25519
→ постоянный host-side root SSH к Linux-гостям

ai_control_ed25519
→ отдельный root SSH credential AI Control

Ansible identity на 311
→ отдельный root SSH credential provisioning
```

`deploy-guest` не создаёт и не ротирует `pve_guest_ed25519`. Он использует public key при создании гостя, а private key — для host-side SSH после запуска.

Stage 1 автоматически не ротирует существующий keypair. Потеря private key при сохранившемся public key считается recovery-ситуацией.

## Initial access: VM

Template `9000` Template-Version 6 использует:

```text
ciuser=root
root password locked
PermitRootLogin prohibit-password
```

Новый Full Clone до первого запуска получает management public keys через Cloud-Init:

```text
clone
→ CPU/RAM/disk/network
→ ciuser=root
→ sshkeys=<public keys>
→ cloud-init update
→ start
→ verify root SSH
```

Template не содержит baked-in authorized_keys.

## Initial access: LXC

Для LXC процедура ещё проще:

```text
resolve Debian 13 appliance
→ собрать файл public keys
→ создать LXC с ssh-public-keys
→ start
→ verify root SSH
```

Proxmox устанавливает переданные public keys для `root` при создании контейнера. Поэтому не требуется промежуточный вход, создание `ops`, настройка sudo или перенос management key.

`base` в этом flow не участвует.

## Несколько SSH identities

Один guest может содержать несколько public keys одного пользователя `root`:

```text
/root/.ssh/authorized_keys
├── pve_guest_ed25519.pub
├── ai_control_ed25519.pub
├── ansible provisioning key
└── personal key при необходимости
```

Это независимые credentials. Удаление AI public key запрещает AI direct SSH, но не затрагивает deployer/Ansible.

Pool `managed` и SSH key membership независимы. Перемещение VM/LXC между pools само по себе не добавляет и не удаляет ключи.

## Bootstrap capabilities

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

- `base` — OS prerequisites после уже работающего root SSH: Python, CA certificates и другие project-wide минимальные настройки;
- `git` — Git там, где он необходим для bootstrap/handoff;
- `docker` — проектный Docker runtime и Compose plugin;
- `ansible_controller` — контейнеризированный Ansible controller / Execution Environment.

Отсутствующая capability считается не запрошенной.

`base: false` или отсутствие `base` **не должно влиять на возможность deployer/AI подключиться к guest по SSH**.

Bootstrap capability должна быть идемпотентной.

## Что запрещено превращать в bootstrap

Bootstrap не является универсальным списком пакетов или приложений.

Не вводить интерфейс вида:

```yaml
bootstrap:
  packages:
    - nginx
    - postgresql
    - redis
```

PostgreSQL, MQTT, Grafana, Gitea, Semaphore, Jenkins и другие прикладные сервисы устанавливаются Ansible/Docker Compose.

## Provisioning capabilities

Для штатной конфигурации ОС планируется отдельная декларация высокого уровня:

```yaml
provisioning:
  capabilities:
    base: true
    docker: true
    git: true
    monitoring: true
```

Общий resolver строит effective desired state, после чего provisioning-слой передаёт capabilities в универсальный Ansible playbook как variables.

Знания о конкретной установке и настройке ПО живут в Ansible roles, а не в `guest.yaml` и не в deployer.

## Специальный bootstrap 311-dev-services

`311-dev-services` — штатный Ansible/control node для Linux-гостей.

Первоначальный flow:

```text
deploy-guest 311 --apply
→ создать LXC 311 с root SSH public keys
→ запустить
→ verify root SSH
→ optional base
→ Git
→ Docker
→ Ansible controller
→ передать управление Ansible
```

Если `base` не нужен, management access всё равно уже существует.

Сам Ansible не должен устанавливаться через `apt install ansible` как основная модель. Целевая структура:

```text
311-dev-services
├── Debian 13
├── root SSH key-only
├── Git
├── Docker Engine + Compose plugin
├── checkout проекта proxmox
└── контейнеризированный Ansible Execution Environment
```

Semaphore, Git service, CI и другие сервисы 311 не относятся к bootstrap и должны разворачиваться последующим provisioning.

## Обычные гости

Для обычных VM/LXC deployer создаёт Proxmox guest и гарантирует management access. Optional bootstrap выполняется только если он действительно запрошен.

Дальнейшая схема:

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

На target-гостях Ansible устанавливать не требуется. Для штатного управления достаточно SSH root по отдельному provisioning key и prerequisites конкретных Ansible modules.

Host-side `pve_guest_ed25519`, AI key и Ansible key не подменяют друг друга.

## Docker workloads

Прикладные контейнеры не описываются внутри bootstrap и не должны реализовываться самим deployer.

В дальнейшем manifest может содержать декларацию workload высокого уровня, но `compose.yaml`, templates, environment contract и логика установки должны жить в проектных файлах и применяться Ansible/Compose.

## Реализация bootstrap interface

Предпочтительная внутренняя модель deployer:

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

Initial SSH injection/verification находится в ядре `deploy-guest.py`, а **не** в `bootstrap/base.py`.

`scripts/guest_config.py` остаётся единым resolver source → effective state для validator и deployer.

## Безопасность

- secrets, private keys, passwords и tokens не хранятся в `guest.yaml`;
- root password locked, password SSH disabled;
- bootstrap handlers не печатают secrets;
- `pve_guest_ed25519` хранится на PVE и читается `pvedeploy`;
- AI и Ansible имеют отдельные private keys;
- deployer не генерирует и не ротирует infrastructure SSH credentials;
- GitHub Deploy Key не используется для SSH в гостей;
- Ansible EE получает только минимально необходимые credentials;
- Docker socket не передаётся Ansible-контейнеру по умолчанию;
- приложение не получает bootstrap capability только ради удобства установки.

## Итоговая цепочка

```text
PVE Stage 1
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
   universal provision playbook
             ↓
       Ansible roles
             ↓
     Docker Compose workloads
```

Главное правило: **management SSH является частью deploy readiness. Bootstrap capabilities работают только после него и никогда не являются условием получения доступа к guest.**
