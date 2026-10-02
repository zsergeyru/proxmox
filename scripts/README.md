# Скрипты

`scripts/` содержит исполняемый код и проверки проекта. Публичный `bootstrap-pve.py` создаёт временный 990, получает закрытый проект и передаёт управление `scripts/bootstrap-runner/bootstrap-host.py`. Вся оркестрация гостя с ролью `infra-manager` находится в этом закрытом репозитории.

## Структура

```text
scripts/
├── validate_repo.py                         # Проверяет структуру репозитория, guest.yaml, схемы, IP-адреса и отсутствие закрытых данных
│
├── bootstrap-runner/                        # Закрытая оркестрация первоначального контура 990
│   ├── bootstrap-host.py                    # Точка входа и общий порядок bootstrap на PVE
│   ├── bootstrap_runner/                    # Разделённая внутренняя логика bootstrap
│   │   ├── persistence.py                   # Постоянные каталоги, bind mount и восстановление состояния
│   │   ├── access.py                        # PVE API и root SSH-доступ временного и постоянного контуров
│   │   ├── infra.py                         # Передача проекта, настройка и проверка infra-manager
│   │   ├── cleanup.py                       # Очистка token, 990 и хостовой поддержки OpenBao
│   │   ├── constants.py                     # Общие константы bootstrap
│   │   └── errors.py                        # Общая ошибка bootstrap
│   ├── prepare-runtime.sh                   # Подготавливает временную среду инструментов внутри 990
│   ├── run-runtime.sh                       # Запускает команды внутри временной среды 990
│   └── deploy-infra-manager.sh              # Вызывает общий deploy-guest для фаз создания и настройки infra-manager
│
├── maintenance/                             # Служебная очистка тестового PVE, не часть штатного bootstrap
│   ├── reset-pve-test-state.sh              # Удаляет только распознанные следы проекта для повторного теста
│   └── reset-pve-clean-slate.sh             # Жёстко очищает тестовый PVE, сохраняя только VM 100
│
├── guests/                                  # Общая логика формирования итоговой конфигурации гостей
│   ├── resolver.py                          # Собирает итоговое состояние из общих настроек, профиля и описания гостя
│   └── render-opentofu-input.py             # Формирует guests.json для OpenTofu из управляемых описаний guest.yaml
│
├── infra-manager/                           # Всё, что относится к управляющей роли infra-manager
│   ├── infra_manager/                       # Основная Python-программа управления и проверки infra-manager
│   │   ├── __init__.py                      # Инициализация пакета Python infra_manager
│   │   ├── __main__.py                      # Точка входа для python3 -m infra_manager
│   │   ├── cli.py                           # Разбирает команды настройки, проверки состояния, доступа к PVE и настройки Semaphore
│   │   ├── common.py                        # Общие ошибки, вывод и безопасный запуск внешних команд
│   │   ├── settings.py                      # Хранит единые неизменяемые пути, имена, версии и значения по умолчанию
│   │   ├── guest_catalog.py                 # Находит специальных гостей по role и читает их VMID, имя и адрес
│   │   ├── semaphore.py                     # Синхронизирует проект, Git, группу переменных и задания Semaphore
│   │   ├── pve.py                           # Проверяет PVE API и полный административный контракт infra-manager
│   │   ├── pve_lifecycle.py                 # Выполняет приёмочную проверку полного жизненного цикла временного LXC 9098
│   │   ├── pve_host.py                      # Выполняет через root SSH операции PVE, недоступные API token
│   │   ├── opentofu.py                      # Выполняет общие операции OpenTofu: подготовку входных данных, инициализацию, план и чтение состояния
│   │   ├── guest_deploy.py                  # Оркестрирует настройку одной гостевой системы и Ansible
│   │   ├── guest_deploy_infrastructure.py   # Планирует и применяет инфраструктурный этап OpenTofu/PVE
│   │   ├── template.py                      # Проверяет общий конфигурационный контракт шаблона VM
│   │   ├── template_build.py                # Собирает Packer-шаблон и запускает итоговую проверку
│   │   ├── template_verify.py               # Проверяет шаблон через временную полную копию 9099
│   │   └── status.py                        # Исполняет проверки и вывод из status.yaml infra-manager
│   │
│   ├── commands/                            # Исходные файлы административных команд, устанавливаемых в /usr/local/sbin
│   │   ├── python-command.sh                # Общая оболочка status, PVE access check и lifecycle test
│   │   ├── activate-runtime.sh              # Отложенно активирует обновлённую управляющую среду
│   │   └── openbao-startup-unseal.sh        # Восстанавливает OpenBao при запуске infra-manager
│   │
│   ├── host/                                # Код, устанавливаемый и выполняемый непосредственно на PVE
│   │   ├── openbao-unseal.py                # Оркестрирует инициализацию, разблокировку и служебные операции OpenBao
│   │   ├── openbao_host/                    # Встроенные программы, TLS и общие ошибки хостовой поддержки OpenBao
│   │   └── recovery.py                      # Независимый аварийный контур восстановления infra-manager
│   │
│   └── jobs/                                # Задания, непосредственно запускаемые Semaphore
│       ├── opentofu-plan.py                  # Формирует входные данные OpenTofu и строит только план изменений
│       ├── deploy-guest.py                   # Разворачивает или приводит выбранную гостевую систему к описанному состоянию
│       ├── build-template.py                 # Собирает Packer-шаблон VM 9000 и запускает его проверку
│       ├── initialize-openbao.py             # Инициализирует и проверяет OpenBao через доверенный PVE
│       └── sync-ssh-access.py                # Синхронизирует SSH OTP/AppRole по access.yaml
│
├── acceptance/                              # Сквозные приёмочные проверки работающей инфраструктуры
│   └── verify-ssh-access.py                  # Проверяет разрешённые и запрещённые SSH OTP-связи
│
└── tests/                                    # Локальные и автоматические проверки без постоянных изменений инфраструктуры
    ├── test-guest-deploy.py                  # Проверяет планирование и безопасное применение изменений гостевой системы
    ├── test-guest-resolver.py                # Проверяет сборщик конфигурации, управление, начальную настройку и возможности профиля
    ├── test-infra-manager-python.py          # Проверяет основу Python-пакета, командную оболочку и вспомогательные функции PVE
    ├── test-pve-lifecycle.py                 # Проверяет lifecycle test без реального изменения PVE
    ├── test-python-command.py                # Поведенчески проверяет общую оболочку трёх административных команд
    ├── test-opentofu-input.py                # Проверяет состав guests.json и исключение специальных объектов
    ├── test-status.py                        # Проверяет снимок, шаблоны и контракты Semaphore
    ├── test-template.py                      # Проверяет параметры и валидацию шаблона Packer
    ├── test-infra-manager-guest-contract.sh  # Проверяет описание гостя и общий Ansible-контур
    ├── test-infra-manager-openbao-contract.sh # Проверяет OpenBao, восстановление и runtime-связи
    ├── test-infra-manager-tooling-contract.sh # Проверяет OpenTofu, Semaphore, status и Compose
    ├── test-infra-manager-ssh-contract.sh    # Проверяет SSH-доверие и безопасную активацию
    └── test-infra-manager-contract.sh        # Локально запускает все четыре контрактные проверки
```

Старого контура `scripts/pve/`, `sync-management-keys.py` и PVE Configuration в действующем коде нет.

## `maintenance/`

Эти сценарии используются только для ручной подготовки тестового PVE. Они намеренно находятся в закрытом репозитории и не входят в публичную точку входа.

## `infra-manager/`


Это код специального управляющего гостя с `role: infra-manager`. Текущий VMID этого гостя — `910`, но общая логика не должна зависеть от этого числа.

### Точки входа

`scripts/bootstrap-runner/bootstrap-host.py` выполняется на физическом PVE после того, как публичный сценарий получил закрытый проект через 990. Дополнительной shell-оболочки для него нет: при ручной диагностике файл запускается напрямую через `python3`. Файл хранит точку входа и общий порядок действий, а работа с постоянными каталогами, доступами, передачей infra-manager и очисткой разделена по модулям `bootstrap_runner/`.

Временный и постоянный доступ к PVE подготавливает непосредственно закрытый `bootstrap-host.py`: он создаёт API token без разделения привилегий, передаёт PVE CA и отдельный root SSH-ключ. Отдельные shell-сценарии доступа больше не используются. Операции, которые Proxmox запрещает API token, выполняет общий модуль `infra_manager/pve_host.py`.

Настройка ОС и служб infra-manager выполняется только через общий `provision.yaml` и общий Ansible playbook. Отдельного `setup.sh` и `infra_manager/setup.py` больше нет.

### Python-пакет `infra_manager/`

`semaphore.py` — создаёт и синхронизирует проект `Proxmox Infrastructure`, ключ доступа GitHub, репозиторий, группы `OpenTofu PVE` и `Infra Manager`, а также задания Semaphore. Группа `Infra Manager` хранит общий переключатель `INFRA_LOG_LEVEL` для краткого, подробного или минимального вывода.

`access.py` — строит эффективные межмашинные SSH-связи из `infrastructure/security/access.yaml` и гостевых описаний.

`jobs/sync-ssh-access.py` синхронизирует только OpenBao SSH OTP/AppRole из `access.yaml`. Настройка конкретного гостя выполняется `deploy-guest.py`, а сквозная проверка разрешённых и запрещённых SSH OTP-связей вынесена в `scripts/acceptance/verify-ssh-access.py`.

`pve.py` — проверяет фактические права ключа доступа PVE API и отсутствие запрещённых административных полномочий.

`status.py` — исполняет `status.yaml` гостя, найденного по `role: infra-manager`. В нём находятся порядок проверок, подписи, источники фактических данных и секции полного экрана; Python содержит только разрешённые способы выполнить проверку или получить значение.

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

Для гостя с `role: infra-manager` используется тот же вход:

```text
Semaphore: Deploy Guest <VMID infra-manager>
→ scripts/infra-manager/jobs/deploy-guest.py
→ проверить существующий infra-manager без собственного OpenTofu state
→ общий Ansible применяет provision.yaml
→ отложенная активация infra-runtime после завершения задания
```

Таким образом, отдельного оркестратора обновления infra-manager нет. Отличается только владение объектом Proxmox и безопасный момент перезапуска контейнера, внутри которого выполняется Semaphore.

`initialize-openbao.py`:

```text
Semaphore: Initialize OpenBao <VMID infra-manager>
→ scripts/infra-manager/jobs/initialize-openbao.py
→ infra_manager.openbao
→ root SSH к PVE
→ установка хостового сценария разблокировки
→ первичная инициализация OpenBao
→ сохранение ключа снятия блокировки только на PVE
→ проверка sealed=false
```

Задание идемпотентно. Первоначальный bootstrap вызывает ту же инициализацию автоматически до удаления временных credentials; отдельный запуск Semaphore остаётся штатным способом повторной проверки и восстановления конфигурации OpenBao.

При запуске самого 910 используется отдельная одноразовая команда `openbao-startup-unseal.sh`. Она ждёт доступности OpenBao и требует рабочее состояние `initialized=true, sealed=false`. Для любого уже инициализированного OpenBao команда вызывает хостовый сценарий на PVE: при `sealed=true` он снимает блокировку, а при `sealed=false` пропускает повторную разблокировку, но всё равно восстанавливает рабочие секреты и выполняет обязательные проверки. Неинициализированный OpenBao считается ошибкой запуска; автоматический `sys/init` не выполняется. Периодического запуска на PVE нет.

`build-template.py`:

```text
Semaphore: Build Template 9000
→ scripts/infra-manager/jobs/build-template.py 9000
→ Packer
→ tpl-debian13
→ infra_manager.template_verify
```

`template_verify.py` вызывается сборкой напрямую: создаёт временную полную копию 9099, проверяет Cloud-Init, QEMU Guest Agent, SSH, machine-id и SSH host keys, затем удаляет клон.

## `infra-manager/commands/`

Это стабильные административные команды, которые общий Ansible-механизм устанавливает в `/usr/local/sbin`.

Один файл `python-command.sh` устанавливается под тремя именами:

```text
infra-manager-status
infra-manager-pve-access-check
infra-manager-pve-lifecycle-test
```

Оболочка определяет нужную Python-подкоманду по своему установленному имени и запускает `python3 -m infra_manager`. Поэтому поиск установленного Python-пакета, настройка `PYTHONPATH` и аварийный переход к рабочей копии проекта описаны только один раз.

Реальная логика lifecycle test находится в `infra_manager/pve_lifecycle.py` и использует общий `PveClient`. Команда только с `--apply` создаёт временный LXC 9098 в pool `managed`, изменяет его, запускает, останавливает и удаляет; аварийная очистка удаляет объект только после проверки ожидаемых типа и имени.

`activate-runtime.sh` устанавливается как `infra-manager-activate-runtime` и используется только для отложенной активации новой управляющей среды после самообновления 910.

## `guests/`

`resolver.py` — единый сборщик конфигурации гостей. Он объединяет `defaults + profile + guest`, вычисляет административный IP-адрес, нормализует `features` и формирует итоговое состояние объекта Proxmox. Права доступа не входят в resolver: они находятся только в `infrastructure/security/access.yaml` и проверяются `validate_repo.py`.

`render-opentofu-input.py` проходит по `infrastructure/guests/*/guest.yaml`, берёт только объекты с `profile` и формирует:

```text
/var/lib/infra-manager/opentofu/guests.json
```

910 имеет общий профиль, но исключается из постоянного OpenTofu-входа по `pve_management: false`; временный bootstrap-контур формирует для него отдельное состояние.

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
- `test-pve-lifecycle.py` проверяет полный сценарий lifecycle test, защиту занятого VMID и аварийную очистку без реального PVE.
- `test-python-command.py` запускает общую оболочку под всеми тремя установленными именами и проверяет фактическую передачу команды в Python CLI.
- `test-bootstrap-host.py` проверяет состояния первоначального контура, восстановление, строгую метку владения 910 и безопасное удаление.
- `test-status.py` проверяет чтение и валидацию состояния Semaphore.
- `test-template.py` проверяет безопасную передачу параметров Packer и валидацию шаблона.
- Контракт infra-manager разделён на `guest`, `openbao`, `tooling` и `ssh`; `test-infra-manager-contract.sh` остаётся только общим локальным запускателем.

## Правило размещения нового кода

- код жизненного цикла 910 → `scripts/infra-manager/`;
- задания Semaphore → `scripts/infra-manager/jobs/`;
- установленные административные команды → `scripts/infra-manager/commands/`;
- общая логика конфигурации гостей → `scripts/guests/`;
- проверки → `scripts/tests/`;
- общая проверка всего репозитория → `scripts/validate_repo.py`.

Не создавать новый параллельный контур развёртывания на PVE.
