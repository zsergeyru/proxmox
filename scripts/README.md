# Скрипты

`scripts/` содержит исполняемый код и проверки проекта. Действующий контур развёртывания один: публичный bootstrap создаёт и подготавливает `910 infra-manager`, после чего управление выполняется кодом из `scripts/infra-manager/`.

## Структура

```text
scripts/
├── validate_repo.py                         # Проверяет структуру репозитория, guest.yaml, схемы, IP-адреса и отсутствие закрытых данных
│
├── guests/                                  # Общая логика формирования итоговой конфигурации гостей
│   ├── resolver.py                          # Собирает итоговое состояние из общих настроек, профиля и описания гостя
│   └── render-opentofu-input.py             # Формирует guests.json для OpenTofu из управляемых описаний guest.yaml
│
├── infra-manager/                           # Всё, что относится к LXC 910 infra-manager
│   ├── setup.sh                             # Точка начальной настройки внутри 910; передаёт управление программе настройки на Python
│   ├── pve-bootstrap-access.sh              # Выполняется на PVE; создаёт ключ доступа к API, назначает права и передаёт доступ в 910
│   │
│   ├── infra_manager/                       # Основная Python-программа управления и проверки 910
│   │   ├── __init__.py                      # Инициализация пакета Python infra_manager
│   │   ├── __main__.py                      # Точка входа для python3 -m infra_manager
│   │   ├── cli.py                           # Разбирает команды настройки, проверки состояния, доступа к PVE и настройки Semaphore
│   │   ├── common.py                        # Общие ошибки, вывод и безопасный запуск внешних команд
│   │   ├── settings.py                      # Хранит единые неизменяемые пути, имена, версии и значения по умолчанию
│   │   ├── setup.py                         # Главный управляющий модуль настройки Debian, Docker, рабочей среды и Semaphore
│   │   ├── semaphore.py                     # Синхронизирует проект, Git, группу переменных и задания Semaphore
│   │   ├── pve.py                           # Проверяет фактические права ключа доступа к API PVE
│   │   ├── opentofu.py                      # Выполняет общие операции OpenTofu: подготовку входных данных, инициализацию, план и чтение состояния
│   │   ├── guest_deploy.py                  # Управляет жизненным циклом развёртывания одной VM и её настройкой через Ansible
│   │   ├── template.py                      # Собирает и проверяет базовый шаблон VM
│   │   └── status.py                        # Проверяет готовность 910 и всех его инструментов
│   │
│   ├── commands/                            # Исходные файлы административных команд, устанавливаемых в /usr/local/sbin
│   │   ├── status.sh                        # Обёртка команды infra-manager-status
│   │   ├── pve-access-check.sh              # Обёртка команды infra-manager-pve-access-check
│   │   └── pve-lifecycle-test.sh            # Приёмочная проверка создания, изменения, запуска, остановки и удаления временного LXC 9098
│   │
│   └── jobs/                                # Задания, непосредственно запускаемые Semaphore
│       ├── opentofu-plan.py                  # Формирует входные данные OpenTofu и строит только план изменений
│       ├── deploy-guest.py                   # Разворачивает или приводит выбранную гостевую систему к описанному состоянию
│       ├── build-template.py                 # Собирает Packer-шаблон VM 9000 и запускает его проверку
│       └── verify-template.py                # Проверяет шаблон 9000 через временную полную копию 9099
│
└── tests/                                    # Локальные и автоматические проверки без постоянных изменений инфраструктуры
    ├── test-guest-resolver.py                # Проверяет сборщик конфигурации, управление, начальную настройку и возможности профиля
    ├── test-infra-manager-python.py          # Проверяет командный интерфейс Python-программы и базовые функции infra-manager
    ├── test-opentofu-input.py                # Проверяет состав guests.json и исключение специальных объектов
    └── test-infra-manager-contract.sh        # Проверяет согласованность 910, Semaphore, PVE, OpenTofu и Packer
```

Старого контура `scripts/pve/`, `sync-management-keys.py` и PVE Configuration в действующем коде нет.

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

`opentofu-plan.py`:

```text
Semaphore: OpenTofu Plan
→ scripts/infra-manager/jobs/opentofu-plan.py
→ scripts/guests/render-opentofu-input.py
→ tofu init
→ tofu plan
```

Задание строит только план и не выполняет `apply` или `destroy`.

`deploy-guest.py`:

```text
Semaphore: Deploy Guest 410
→ scripts/infra-manager/jobs/deploy-guest.py
→ OpenTofu apply для выбранной гостевой системы
→ Ansible-настройка гостевой системы
```

Задание создаёт или обновляет выбранную управляемую гостевую систему по её описанию и затем выполняет настройку через Ansible.

`build-template.py`:

```text
Semaphore: Build Template 9000
→ scripts/infra-manager/jobs/build-template.py 9000
→ Packer
→ tpl-debian13
→ scripts/infra-manager/jobs/verify-template.py 9000
```

`verify-template.py` создаёт временный Full Clone 9099, проверяет Cloud-Init, QEMU Guest Agent, SSH, machine-id и SSH host keys, затем удаляет клон.

## `infra-manager/commands/`

Это стабильные административные команды, которые `setup.py` устанавливает в `/usr/local/sbin`.

`status.sh` устанавливается как:

```text
infra-manager-status
```

`pve-access-check.sh` устанавливается как:

```text
infra-manager-pve-access-check
```

`pve-lifecycle-test.sh` устанавливается как:

```text
infra-manager-pve-lifecycle-test
```

Последний является явным приёмочным тестом и только с `--apply` создаёт временный LXC 9098, изменяет его, запускает, останавливает и удаляет.

## `guests/`

`resolver.py` — единый resolver конфигурации гостей. Он объединяет `defaults + profile + guest`, вычисляет management IP, нормализует `management`, `bootstrap` и features и формирует effective state.

`render-opentofu-input.py` проходит по `infrastructure/guests/*/guest.yaml`, берёт только объекты с `profile` и формирует:

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

- `test-guest-resolver.py` проверяет resolver и Bootstrap-возможности гостей.
- `test-opentofu-input.py` проверяет состав входа OpenTofu и исключение 910.
- `test-infra-manager-python.py` проверяет Python CLI и основные вспомогательные функции infra-manager.
- `test-infra-manager-contract.sh` проверяет согласованность кода 910, Compose, PVE access, Semaphore, OpenTofu и Packer.

## Правило размещения нового кода

- код жизненного цикла 910 → `scripts/infra-manager/`;
- задания Semaphore → `scripts/infra-manager/jobs/`;
- установленные административные команды → `scripts/infra-manager/commands/`;
- общая логика guest-конфигурации → `scripts/guests/`;
- проверки → `scripts/tests/`;
- общая проверка всего репозитория → `scripts/validate_repo.py`.

Не создавать новый параллельный контур развёртывания на PVE.
