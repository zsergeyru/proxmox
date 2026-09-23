# Скрипты

`scripts/` содержит исполняемый код и проверки проекта. Действующий контур развёртывания один: публичный bootstrap создаёт и подготавливает `910 infra-manager`, после чего управление выполняется кодом из `scripts/infra-manager/`.

## Структура

```text
scripts/
├── validate_repo.py
├── guests/
│   ├── guest_config.py
│   └── render-opentofu-input.py
├── infra-manager/
│   ├── setup.sh
│   ├── pve-bootstrap-access.sh
│   ├── infra_manager/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── cli.py
│   │   ├── common.py
│   │   ├── setup.py
│   │   ├── semaphore.py
│   │   ├── pve.py
│   │   └── status.py
│   ├── commands/
│   │   ├── status.sh
│   │   ├── check-pve-access.sh
│   │   └── test-pve-lifecycle.sh
│   └── jobs/
│       ├── opentofu-plan.sh
│       ├── build-template.sh
│       └── test-template.sh
└── tests/
    ├── test-guest-bootstrap.py
    ├── test-infra-manager-python.py
    ├── test-opentofu-input.py
    └── test-setup-contract.sh
```

Старого контура `scripts/pve/`, `deploy-guest.py`, `sync-management-keys.py` и PVE Configuration в действующем коде нет.

## `infra-manager/`

Это код специального LXC `910 infra-manager`.

### Точки входа

`setup.sh` — минимальная оболочка, которую публичный bootstrap запускает внутри 910. Она обеспечивает наличие Python и передаёт управление:

```text
python3 -m infra_manager setup
```

Основная настройка 910 выполняется в `infra_manager/setup.py`.

`pve-bootstrap-access.sh` выполняется публичным bootstrap на физическом PVE. Он подготавливает PVE CA, API token `root@pam!infra-manager`, ACL и безопасную передачу credential в 910.

### Python-пакет `infra_manager/`

`setup.py` — основной оркестратор настройки 910: Debian, Docker, постоянные каталоги, секреты, CA, OpenTofu input, `infra-runtime`, Semaphore и итоговые проверки.

`semaphore.py` — создаёт и синхронизирует проект `Proxmox Infrastructure`, GitHub Deploy Key, репозиторий, Variable Group `OpenTofu PVE` и задания Semaphore.

`pve.py` — проверяет фактические права PVE API token и отсутствие запрещённых административных полномочий.

`status.py` — проверяет готовность Docker, `infra-runtime`, Semaphore, Git-доступа, OpenTofu, Packer, Ansible, proxmoxer и PVE API.

`cli.py`, `__main__.py` и `common.py` образуют общую командную оболочку и вспомогательные функции Python-части.

## `infra-manager/jobs/`

Здесь находятся сценарии, которые запускает Semaphore.

`opentofu-plan.sh`:

```text
Semaphore: OpenTofu Plan
→ scripts/infra-manager/jobs/opentofu-plan.sh
→ scripts/guests/render-opentofu-input.py
→ tofu init
→ tofu plan
```

Задание строит только план и не выполняет `apply` или `destroy`.

`build-template.sh`:

```text
Semaphore: Build Template 9000
→ scripts/infra-manager/jobs/build-template.sh 9000
→ Packer
→ tpl-debian13
→ scripts/infra-manager/jobs/test-template.sh 9000
```

`test-template.sh` создаёт временный Full Clone 9099, проверяет Cloud-Init, QEMU Guest Agent, SSH, machine-id и SSH host keys, затем удаляет клон.

## `infra-manager/commands/`

Это стабильные административные команды, которые `setup.py` устанавливает в `/usr/local/sbin`.

`status.sh` устанавливается как:

```text
infra-manager-status
```

`check-pve-access.sh` устанавливается как:

```text
infra-manager-pve-access-check
```

`test-pve-lifecycle.sh` устанавливается как:

```text
infra-manager-pve-lifecycle-test
```

Последний является явным приёмочным тестом и только с `--apply` создаёт временный LXC 9098, изменяет его, запускает, останавливает и удаляет.

## `guests/`

`guest_config.py` — единый resolver конфигурации гостей. Он объединяет `defaults + profile + guest`, вычисляет management IP, нормализует `management`, `bootstrap` и features и формирует effective state.

`render-opentofu-input.py` проходит по `guests/*/guest.yaml`, берёт только объекты с `profile` и формирует:

```text
/var/lib/infra-manager/opentofu/guests.json
```

Поэтому специальный 910 без `profile` не попадает в OpenTofu.

## `validate_repo.py`

Общий валидатор структуры проекта, `guest.yaml`, `defaults.yaml`, JSON Schema, IP-адресации, effective state и запрета секретов.

Штатный запуск:

```bash
python scripts/validate_repo.py
```

Его также запускает GitHub Actions.

## `tests/`

Все repo-level и contract-тесты собраны в одном каталоге.

- `test-guest-bootstrap.py` проверяет resolver и Bootstrap-возможности гостей.
- `test-opentofu-input.py` проверяет состав входа OpenTofu и исключение 910.
- `test-infra-manager-python.py` проверяет Python CLI и основные вспомогательные функции infra-manager.
- `test-setup-contract.sh` проверяет согласованность кода 910, Compose, PVE access, Semaphore, OpenTofu и Packer.

## Правило размещения нового кода

- код жизненного цикла 910 → `scripts/infra-manager/`;
- задания Semaphore → `scripts/infra-manager/jobs/`;
- установленные административные команды → `scripts/infra-manager/commands/`;
- общая логика guest-конфигурации → `scripts/guests/`;
- проверки → `scripts/tests/`;
- общая проверка всего репозитория → `scripts/validate_repo.py`.

Не создавать новый параллельный контур развёртывания на PVE.
