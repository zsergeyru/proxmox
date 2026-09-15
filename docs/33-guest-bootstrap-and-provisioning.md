# Guest bootstrap и provisioning

## Статус решения

Принята двухуровневая модель настройки Linux-гостей:

```text
deploy-guest.py
→ PVE lifecycle
→ ограниченный bootstrap
→ передача управления Ansible
→ штатный provisioning ОС и приложений
```

Это архитектурное решение. Поля `bootstrap` и `provisioning` пока не входят в действующие guest schema v5 и должны быть добавлены одновременно с реализацией соответствующей поддержки в `deploy-guest.py` и validator.

## Граница ответственности

`deploy-guest.py` отвечает прежде всего за Proxmox-side desired state:

```text
создать/найти VM или LXC
→ CPU / RAM / disk
→ network
→ pool / protection / boot
→ start
→ дождаться готовности management-доступа
```

Дополнительно deployer получает небольшой расширяемый bootstrap-интерфейс. Его задача — только довести гостя до состояния, в котором дальнейшую повторяемую настройку может выполнять штатный provisioning-механизм.

Deployer не должен становиться вторым configuration-management framework.

## Первичный SSH-доступ deployer

Отдельную SSH identity для первичного доступа к новым VM/LXC создаёт **PVE Private Stage 1**, а не `deploy-guest.py`.

Канонические файлы на PVE:

```text
/etc/proxmox-deployer/ssh/guest_bootstrap_ed25519
/etc/proxmox-deployer/ssh/guest_bootstrap_ed25519.pub
```

Разделение credentials:

```text
github_proxmox_repo_ed25519
→ только read-only доступ PVE к GitHub repository

guest_bootstrap_ed25519
→ только первичный host-side SSH/bootstrap новых гостей

Ansible identity на 311
→ отдельный последующий credential штатного provisioning
```

`deploy-guest` не создаёт и не ротирует `guest_bootstrap_ed25519`. Он использует public key при создании гостя штатным для VM/LXC способом, а private key — для первичного SSH-доступа после запуска.

Stage 1 автоматически не ротирует существующий keypair. Если private key существует, `.pub` восстанавливается из него. Если private key потерян, но public key остался, bootstrap останавливается и требует явного recovery, потому что этот public key уже может быть установлен на существующих гостях.

Точный initial-access flow для VM и LXC должен использовать эту одну host-side identity, но не должен смешивать её с GitHub credential или будущей Ansible identity 311.

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

Начальный whitelist bootstrap capabilities:

- `base` — проверить/довести до проектного минимума SSH, `ops`, Python, CA certificates и другие prerequisites, необходимые для дальнейшего управления;
- `git` — обеспечить Git там, где он необходим для bootstrap/handoff;
- `docker` — установить и запустить проектный Docker runtime и Compose plugin;
- `ansible_controller` — подготовить контейнеризированный Ansible controller / Execution Environment.

Отсутствующая capability считается не запрошенной. Не требуется перечислять все возможные capabilities со значением `false`.

Bootstrap capability должна быть идемпотентной: повторный запуск проверяет существующее состояние и выполняет только необходимые изменения.

## Что запрещено превращать в bootstrap

Bootstrap не является универсальным списком пакетов или приложений.

Не вводить общий интерфейс вида:

```yaml
bootstrap:
  packages:
    - nginx
    - postgresql
    - redis
```

и не описывать через bootstrap прикладные Docker-контейнеры.

Критерий добавления новой bootstrap capability:

> capability нужна для первичного запуска гостя или для передачи дальнейшего управления штатному provisioning-механизму.

Поэтому `base`, `git`, `docker`, `ansible_controller` допустимы, а PostgreSQL, MQTT, Grafana, Gitea, Semaphore, Jenkins и другие прикладные сервисы должны устанавливаться Ansible/Docker Compose.

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

Это не нативный синтаксис Ansible, а проектный manifest contract.

Общий resolver строит effective desired state, после чего provisioning-слой передаёт capabilities в один универсальный Ansible playbook как variables. Playbook условно подключает стандартные Ansible roles.

Принцип:

```text
provisioning.capabilities.docker=true
→ include_role: docker

provisioning.capabilities.git=true
→ include_role: git
```

Знания о конкретной установке и настройке ПО живут в Ansible roles, а не в `guest.yaml` и не в deployer.

## Специальный bootstrap 311-dev-services

`311-dev-services` — штатный Ansible/control node для Linux-гостей.

Проблема bootstrap заключается в том, что Ansible ещё недоступен в момент первоначального создания самого 311. Поэтому для 311 используется ограниченный bootstrap deployer:

```text
deploy-guest 311 --apply
→ создать LXC 311
→ запустить
→ установить первичный management SSH-доступ через guest-bootstrap identity PVE
→ base
→ Git
→ Docker
→ Ansible controller
→ передать управление Ansible
```

Сам Ansible не должен устанавливаться в Debian 311 через `apt install ansible` как основная модель.

Целевая структура 311:

```text
311-dev-services
├── Debian 13
├── SSH / ops
├── Git
├── Docker Engine + Compose plugin
├── checkout проекта proxmox
└── контейнеризированный Ansible Execution Environment
```

Ansible Execution Environment должен содержать согласованный набор `ansible-core`, `ansible-runner`, collections и Python dependencies. Версии должны быть воспроизводимыми и задаваться проектом.

После bootstrap Ansible может донастроить и сам 311 тем же штатным способом, которым конфигурируются остальные Linux-гости.

Semaphore, Git service, CI и другие сервисы 311 не относятся к bootstrap и должны разворачиваться последующим provisioning.

## Обычные гости

Для обычных VM/LXC deployer создаёт Proxmox guest и выполняет только минимально необходимый bootstrap, если он действительно нужен.

Дальнейшая схема:

```text
311 / Ansible EE
      │
      ├─ SSH → 109
      ├─ SSH → 211
      ├─ SSH → 301
      ├─ SSH → 311
      ├─ SSH → 321
      ├─ SSH → 331
      └─ SSH → 401
```

На управляемых гостях Ansible устанавливать не требуется. Для штатного управления достаточно SSH, пользователя `ops`, Python и необходимых privilege escalation prerequisites.

Host-side `guest_bootstrap_ed25519` нужен для первичного handoff. После появления Ansible штатное повторяемое управление не должно зависеть от использования этого ключа как Ansible credential.

## Docker workloads

Прикладные контейнеры не описываются внутри bootstrap и не должны реализовываться самим deployer.

В дальнейшем manifest может содержать декларацию workload высокого уровня, например:

```yaml
workloads:
  mqtt:
    type: compose
    enabled: true
```

но сам `compose.yaml`, templates, environment contract и логика установки должны храниться в проектных файлах и применяться Ansible/Compose. `guest.yaml` не должен превращаться в самодельную замену Docker Compose.

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

Ядро deployer работает только с известным whitelist capabilities и вызывает соответствующие идемпотентные bootstrap handlers. Добавление новой capability не должно требовать копирования логики resolver или PVE lifecycle.

`scripts/guest_config.py` остаётся единым resolver source → effective state для validator и deployer.

## Безопасность

- secrets, private keys, passwords и tokens не хранятся в `guest.yaml`;
- bootstrap handlers не должны печатать secrets в PLAN/log;
- `guest_bootstrap_ed25519` хранится только на PVE и читается `pvedeploy`;
- deployer не генерирует и не ротирует infrastructure SSH credentials;
- GitHub Deploy Key не используется для SSH в гостей;
- Ansible EE получает только минимально необходимые mounts/credentials;
- Docker socket не передаётся Ansible-контейнеру по умолчанию;
- приложение или сервис не получает bootstrap capability только ради удобства установки;
- прямой SSH остаётся аварийным каналом, но не основным способом повторяемой настройки.

## Итоговая цепочка

```text
PVE Stage 1
   └─ guest_bootstrap_ed25519

            ↓

guest.yaml
   ↓
scripts/guest_config.py
   ↓
effective desired state
   ↓
deploy-guest.py
   ├─ PVE PLAN/APPLY
   ├─ inject guest-bootstrap public key
   └─ limited bootstrap capabilities
             ↓
       Ansible EE на 311
             ↓
   universal provision playbook
             ↓
       Ansible roles
             ↓
     Docker Compose workloads
```

Главное правило: **Stage 1 создаёт постоянную host-side bootstrap identity; deployer использует её и создаёт инфраструктурную основу/guest bootstrap; Ansible отвечает за повторяемое состояние Linux и приложений.**
