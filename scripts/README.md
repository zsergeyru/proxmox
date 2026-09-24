# Скрипты

`scripts/` содержит исполняемый код и проверки проекта. Действующий контур развёртывания один: публичный начальный скрипт создаёт и подготавливает `910 infra-manager`, после чего управление выполняется кодом из `scripts/infra-manager/`.

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
│   │   ├── setup.py                         # Оркестрирует последовательность полной настройки 910
│   │   ├── setup_context.py                 # Хранит общий контекст настройки и отвечает за журнал и сообщения этапов
│   │   ├── host_setup.py                    # Готовит Debian, Docker, каталоги, ключи, CA и локальные команды
│   │   ├── runtime_setup.py                 # Разворачивает Compose, Semaphore и инструменты среды выполнения infra-runtime
│   │   ├── semaphore.py                     # Синхронизирует проект, Git, группу переменных и задания Semaphore
│   │   ├── pve.py                           # Проверяет фактические права ключа доступа к API PVE
│   │   ├── opentofu.py                      # Выполняет общие операции OpenTofu: подготовку входных данных, инициализацию, план и чтение состояния
│   │   ├── guest_deploy.py                  # Управляет жизненным циклом развёртывания одной VM и её настройкой через Ansible
│   │   ├── template.py                      # Проверяет общий конфигурационный контракт шаблона VM
│   │   ├── template_build.py                # Собирает Packer-шаблон и запускает итоговую проверку
│   │   ├── template_verify.py               # Проверяет шаблон через временную полную копию 9099
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
    ├── test-guest-deploy.py                  # Проверяет планирование и безопасное применение изменений гостевой системы
    ├── test-guest-resolver.py                # Проверяет сборщик конфигурации, управление, начальную настройку и возможности профиля
    ├── test-infra-manager-python.py          # Проверяет основу Python-пакета, командную оболочку и вспомогательные функции PVE
    ├── test-opentofu-input.py                # Проверяет состав guests.json и исключение специальных объектов
    ├── test-setup.py                         # Проверяет единые настройки и композицию этапов установки
    ├── test-status.py                        # Проверяет снимок и контракты состояния Semaphore
    ├── test-template.py                      # Проверяет параметры и валидацию шаблона Packer
    └── test-infra-manager-contract.sh        # Проверяет согласованность 910, Semaphore, PVE, OpenTofu и Packer
```

Старого контура `scripts/pve/`, `sync-management-keys.py` и PVE Configuration в действующем коде нет.

## `infra-manager/`

Это код специального LXC `910 infra-manager`.

### Точки входа

`setup.sh` — минимальная оболочка, которую публичный начальный скрипт запускает внутри 910. Она обеспечивает наличие Python и передаёт управление:

```text
python3 -m infra_manager setup
```

Основная настройка 910 запускается через `infra_manager/setup.py`.

`pve-bootstrap-access.sh` выполняется публичным начальным скриптом на физическом PVE. Он подготавливает PVE CA, ключ доступа API `root@pam!infra-manager`, ACL и безопасную передачу учётных данных в 910.

### Python-пакет `infra_manager/`

`setup.py` задаёт последовательность полной настройки 910 и общий контекст выполнения. Подготовка Debian, Docker, каталогов, ключей и CA находится в `host_setup.py`; Compose, входные данные OpenTofu, `infra-runtime` и Semaphore — в `runtime_setup.py`.

`semaphore.py` — создаёт и синхронизирует проект `Proxmox Infrastructure`, ключ доступа GitHub, репозиторий, группу переменных `OpenTofu PVE` и задания Semaphore.

`pve.py` — проверяет фактические права ключа доступа PVE API и отсутствие запрещённых административных полномочий.

`status.py` — проверяет готовность Docker, `infra-runtime`, Semaphore, доступа к Git, OpenTofu, Packer, Ansible, proxmoxer и PVE API.

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

`verify-template.py` создаёт временную полную копию 9099, проверяет Cloud-Init, QEMU Guest Agent, SSH, machine-id и SSH host keys, затем удаляет клон.

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

`resolver.py` — единый сборщик конфигурации гостей. Он объединяет `defaults + profile + guest`, вычисляет административный IP-адрес, нормализует параметры `management`, `bootstrap` и `features` и формирует итоговое состояние.

`render-opentofu-input.py` проходит по `infrastructure/guests/*/guest.yaml`, берёт только объекты с `profile` и формирует:

```text
/var/lib/infra-manager/opentofu/guests.json
```

Поэтому специальный 910 без `profile` не попадает в OpenTofu.

## `validate_repo.py`

Общая проверка структуры проекта, `guest.yaml`, `defaults.yaml`, JSON Schema, IP-адресации, итогового состояния и запрета секретов.

Штатный запуск:

```bash
python scripts/validate_repo.py
```

Его также запускает GitHub Actions.

## `tests/`

Все проверки уровня репозитория и контрактов собраны в одном каталоге.

- `test-guest-resolver.py` проверяет сборщик конфигурации и возможности начальной настройки гостей.
- `test-guest-deploy.py` проверяет планирование, сверку состояния и безопасное применение изменений гостя.
- `test-opentofu-input.py` проверяет состав входа OpenTofu и исключение 910.
- `test-infra-manager-python.py` проверяет основу Python-пакета, командную оболочку и PVE-вспомогательные функции.
- `test-setup.py` проверяет общие пути, версии и композицию этапов установки.
- `test-status.py` проверяет чтение и валидацию состояния Semaphore.
- `test-template.py` проверяет безопасную передачу параметров Packer и валидацию шаблона.
- `test-infra-manager-contract.sh` проверяет согласованность кода 910, Compose, доступ PVE, Semaphore, OpenTofu и Packer.

## Правило размещения нового кода

- код жизненного цикла 910 → `scripts/infra-manager/`;
- задания Semaphore → `scripts/infra-manager/jobs/`;
- установленные административные команды → `scripts/infra-manager/commands/`;
- общая логика конфигурации гостей → `scripts/guests/`;
- проверки → `scripts/tests/`;
- общая проверка всего репозитория → `scripts/validate_repo.py`.

Не создавать новый параллельный контур развёртывания на PVE.
