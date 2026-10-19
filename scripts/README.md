# Скрипты

`scripts/` содержит исполняемый код и проверки проекта. Публичный `bootstrap-pve.py` создаёт временный 990, получает основной проект и передаёт управление `scripts/bootstrap-runner/bootstrap-host.py`. Вся оркестрация гостя с ролью `infra-manager` находится в этом основном репозитории.

## Структура

Список отражает фактические файлы ветки `main`. В каталоге `tests/` перечислены названия проверок; их назначение приведено ниже.

```text
scripts/
├── acceptance/  # Проверки на работающей инфраструктуре
│   └── verify-ssh-access.py  # Проверка разрешённых и запрещённых связей SSH OTP
├── bootstrap-runner/  # Первоначальная установка и восстановление через 990
│   ├── bootstrap_runner/
│   │   ├── __init__.py  # Инициализация пакета
│   │   ├── access.py  # Первоначальные права доступа к PVE
│   │   ├── cleanup.py  # Удаление временных полномочий и ресурсов
│   │   ├── commands.py  # Выполнение внешних команд и управление LXC
│   │   ├── constants.py  # Общие константы
│   │   ├── errors.py  # Общие ошибки
│   │   ├── infra.py  # Развёртывание и настройка управляющего гостя
│   │   ├── paths.py  # Расположение файлов и каталогов
│   │   ├── persistence.py  # Постоянное состояние и подключения каталогов
│   │   └── role.py  # Поиск управляющего гостя по роли
│   ├── bootstrap-host.py  # Главный сценарий первоначальной установки на PVE
│   ├── prepare-runtime.sh  # Подготовка инструментов во временном 990
│   └── run-runtime.sh  # Запуск этапов настройки внутри 990
├── guests/  # Сборка итоговых настроек гостей
│   ├── render-opentofu-input.py  # Формирование входных данных OpenTofu
│   └── resolver.py  # Объединение общих настроек, профиля и гостя
├── infra-manager/  # Управление гостями, службами и PVE
│   ├── commands/  # Устанавливаемые команды управляющего гостя
│   │   ├── activate-runtime.sh  # Активация новой рабочей среды
│   │   ├── openbao-startup-unseal.sh  # Разблокировка и проверка OpenBao при запуске
│   │   ├── operator.sh  # Запуск операторского слоя
│   │   └── python-command.sh  # Выполнение внутренних Python-команд
│   ├── host/  # Программы, устанавливаемые непосредственно на PVE
│   │   ├── openbao_host/  # Поддержка OpenBao на PVE
│   │   │   ├── __init__.py  # Инициализация пакета
│   │   │   ├── errors.py  # Общие ошибки
│   │   │   ├── snippets.py  # Встроенные программы поддержки OpenBao
│   │   │   └── tls.py  # Материалы TLS OpenBao на PVE
│   │   ├── guest-storage.py  # Создание и подключение постоянных томов LXC на PVE
│   │   ├── manager.py  # Операторская оболочка PVE
│   │   ├── openbao-unseal.py  # Привилегированные операции OpenBao через PVE
│   │   └── recovery.py  # Аварийные действия и восстановление
│   ├── infra_manager/  # Основные программные модули
│   │   ├── __init__.py  # Инициализация пакета
│   │   ├── __main__.py  # Запуск пакета как модуля
│   │   ├── access.py  # Первоначальные права доступа к PVE
│   │   ├── cli.py  # Команды настройки и проверки
│   │   ├── common.py  # Ошибки, вывод и запуск внешних команд
│   │   ├── guest_catalog.py  # Поиск гостей и чтение описаний
│   │   ├── guest_deploy_infrastructure.py  # Изменения инфраструктуры через OpenTofu/PVE
│   │   ├── guest_deploy.py  # Настройка гостя через Ansible
│   │   ├── guest_operations.py  # Операции deploy/status/repair/sync
│   │   ├── guest_ssh.py  # Единый административный SSH-сеанс через OpenBao
│   │   ├── guest_status.py  # Проверка состояния, включая сертификатный SSH-вход
│   │   ├── openbao.py  # Первичная настройка OpenBao
│   │   ├── opentofu.py  # Подготовка плана и состояния OpenTofu
│   │   ├── operation_lock.py  # Блокировка одновременных операций
│   │   ├── operator.py  # Логика команд управляющего гостя
│   │   ├── portal_gateway.py  # Ограниченный запуск действий из Homepage
│   │   ├── portal.py  # Описания служб и страниц портала
│   │   ├── pve_host.py  # Отдельный служебный SSH-доступ к PVE
│   │   ├── pve_lifecycle.py  # Проверка жизненного цикла испытательного LXC
│   │   ├── pve.py  # Доступ к PVE API
│   │   ├── recovery.py  # Аварийные действия и восстановление
│   │   ├── semaphore.py  # Проекты и задания Semaphore
│   │   ├── settings.py  # Пути и основные настройки
│   │   ├── status.py  # Глубокая проверка infra-manager
│   │   ├── template_build.py  # Сборка шаблона Packer
│   │   ├── template_verify.py  # Проверка шаблона через испытательную VM 9099
│   │   └── template.py  # Проверка настроек шаблона
│   └── jobs/  # Точки запуска заданий и внутренних процедур
│       ├── bootstrap-infra-manager.py  # Этапы первоначального развёртывания 910
│       ├── build-template.py  # Создание и проверка шаблона 9000
│       ├── check-runtime-downloads.py  # Проверка доступности источников загрузки
│       ├── guest-operation.py  # Общая точка входа операций над гостями
│       ├── initialize-openbao.py  # Первоначальная настройка OpenBao
│       ├── opentofu-plan.py  # Формирование плана OpenTofu
│       ├── set-log-level.py  # Переключение подробности журнала
│       └── sync-ssh-access.py  # Согласование SSH OTP и AppRole с access.yaml
├── maintenance/  # Очистка испытательного PVE
│   ├── reset-pve-clean-slate.sh  # Полная очистка испытательного PVE с сохранением VM 100
│   └── reset-pve-test-state.sh  # Очистка распознанных следов испытаний
├── tests/  # Автоматические проверки без постоянных изменений PVE
│   ├── test-access-contract.py
│   ├── test-ai-control-contract.py
│   ├── test-bootstrap-architecture.sh
│   ├── test-bootstrap-host.py
│   ├── test-guest-storage.py
│   ├── test-guest-deploy.py
│   ├── test-guest-operations.py
│   ├── test-guest-resolver.py
│   ├── test-guest-ssh.py
│   ├── test-guest-status.py
│   ├── test-infra-manager-contract.sh
│   ├── test-infra-manager-guest-contract.sh
│   ├── test-infra-manager-openbao-contract.sh
│   ├── test-infra-manager-python.py
│   ├── test-infra-manager-ssh-contract.sh
│   ├── test-infra-manager-tooling-contract.sh
│   ├── test-manager-host.py
│   ├── test-network-gateway-contract.py
│   ├── test-openbao-live.py
│   ├── test-openbao-otp-client.py
│   ├── test-openbao-otp-verify.py
│   ├── test-openbao-ssh.py
│   ├── test-openbao.py
│   ├── test-opentofu-input.py
│   ├── test-portal.py
│   ├── test-pve-host.py
│   ├── test-pve-lifecycle.py
│   ├── test-python-command.py
│   ├── test-recovery-host.py
│   ├── test-runtime-downloads.py
│   ├── test-status.py
│   └── test-template.py
├── README.md  # Описание скриптов и точек запуска
└── validate_repo.py  # Общая проверка структуры и правил репозитория
```

Старого контура `scripts/pve/`, `sync-management-keys.py` и PVE Configuration в действующем коде нет.

## `maintenance/`

Эти сценарии используются только для ручной подготовки тестового PVE. Они намеренно находятся в основном репозитории и не входят в публичную точку входа.

## `infra-manager/`


Это код специального управляющего гостя с `role: infra-manager`. Текущий VMID этого гостя — `910`, но общая логика не должна зависеть от этого числа.

### Точки входа

`scripts/bootstrap-runner/bootstrap-host.py` выполняется на физическом PVE после того, как публичный сценарий получил основной проект через 990. Дополнительной shell-оболочки для него нет: при ручной диагностике файл запускается напрямую через `python3`. Файл хранит точку входа и общий порядок действий, а работа с постоянными каталогами, доступами, передачей infra-manager и очисткой разделена по модулям `bootstrap_runner/`.

Временный и постоянный доступ к PVE подготавливает непосредственно основной `bootstrap-host.py`: он создаёт API token без разделения привилегий, передаёт PVE CA и отдельный root SSH-ключ. Отдельные shell-сценарии доступа больше не используются. Операции, которые Proxmox запрещает API token, выполняет общий модуль `infra_manager/pve_host.py`. При наличии раздела `pve_storage.mounts` в описании гостя исполнитель читает физические размеры и пути из `infrastructure/hosts/pve/storage.yaml` и вызывает на PVE `host/guest-storage.py`: тот безопасно подготавливает постоянные тома и подключения LXC до Ansible. Защищённые каталоги управляющего гостя создаёт первоначальный установщик независимо от этого файла. Существующие тома не форматируются и не удаляются.

Настройка ОС и служб infra-manager выполняется только через общий `provision.yaml` и общий Ansible playbook. Отдельного `setup.sh` и `infra_manager/setup.py` больше нет.

### Python-пакет `infra_manager/`

`guest_ssh.py` — общий класс `GuestSshSession` для административного SSH-доступа к управляемым гостям. Штатные команды `deploy` и `status`, а также Ansible используют временный клиентский ключ, сертификат OpenBao и проверку сервера через SSH Host CA. Временные материалы удаляются после операции; устаревшие записи обычных ключей гостей в постоянном `known_hosts` не используются. Для нового гостя существует явный режим первоначального доступа с одноразовым ключом и заранее подтверждённым ключом сервера; автоматическое ослабление проверки при сбое штатного доступа запрещено. Отдельный доступ к PVE и межмашинный SSH OTP в этот класс не входят.

`semaphore.py` — создаёт и синхронизирует проект `Proxmox Infrastructure`, ключ доступа GitHub, репозиторий, группы `OpenTofu PVE` и `Infra Manager`, а также задания Semaphore. Группа `Infra Manager` хранит общий переключатель `INFRA_LOG_LEVEL` для обычного, подробного или минимального вывода. Оператор меняет его через отдельное задание `Set Log Level` с выпадающим списком, а не ручным редактированием Variable Group.

`access.py` — строит эффективные межмашинные SSH-связи из `infrastructure/security/access.yaml` и гостевых описаний.

`jobs/sync-ssh-access.py` синхронизирует только OpenBao SSH OTP/AppRole из `access.yaml`. Настройка конкретного гостя запускается через `jobs/guest-operation.py` (операция `deploy`), а сквозная проверка разрешённых и запрещённых SSH OTP-связей вынесена в `scripts/acceptance/verify-ssh-access.py`.

`pve.py` — проверяет фактические права ключа доступа PVE API и отсутствие запрещённых административных полномочий.

`portal_gateway.py` — ограниченный HTTP-посредник Homepage. Он принимает только `status/sync/repair`, требует подтверждение POST с той же страницы, запускает установленную операторскую команду 910 без `--trusted-pve` и потоково показывает экранированный журнал в браузере. Semaphore для этого маршрута не используется.

`guest_status.py` — общий механизм проверки состояния гостей: проверяет PVE и реальный SSH-вход по сертификату (а не только открытый порт), опубликованные службы из `provision.yaml`, дополнительные проверки из общего `status.yaml` и формирует краткий или полный вывод. По умолчанию секреты не читаются. Задания Semaphore и Homepage всегда используют этот безопасный режим; показать операторские пароли может только явный доверенный вызов с PVE.

`status.py` — внутренний исполнитель глубокой проверки `infra-manager`. Он читает установленный `/etc/infra-manager/status.yaml`, который строится из локального `infra-manager-status.yaml`; операторский вывод служб и паролей в него больше не входит.

`cli.py`, `__main__.py` и `common.py` образуют общую командную оболочку и вспомогательные функции Python-части. Чтение файлов переменных окружения выполняет одна функция `common.read_env_file`, YAML-описания гостей — `guest_catalog.load_manifest`, а настройки root SSH-доступа к PVE задаются единственным сборщиком в `pve_host.py`.

## `infra-manager/jobs/`

Здесь находятся точки входа заданий Semaphore и служебные процедуры первоначальной установки.

`opentofu-plan.py`:

```text
Semaphore: OpenTofu Plan
→ scripts/infra-manager/jobs/opentofu-plan.py
→ scripts/guests/render-opentofu-input.py
→ tofu init
→ tofu plan
```

Задание строит только план и не выполняет `apply` или `destroy`.

`guest-operation.py` является общей точкой входа четырёх отдельных шаблонов Semaphore:

```text
Deploy Guest ─┐
Status Guest ─┤
Repair Guest ─┤
Sync Guest ───┘→ guest-operation.py → guest_operations.py
```

Во всех шаблонах гость выбирается только из `GUEST_VMID` типа `enum`. Все четыре операции используют общий каталог гостей с `guest.yaml + provision.yaml`.

`Deploy Guest` вызывает общий OpenTofu + Ansible-контур. Для `role: infra-manager` он сохраняет прежнее правило отложенной активации `infra-runtime`.

`bootstrap-infra-manager.py` используется только начальным контуром 990 для последовательных этапов создания и настройки infra-manager. Обычное развёртывание и самообновление выполняются через `guest-operation.py`; прежняя отдельная точка `deploy-guest.py` удалена как дублирующая.

`check-runtime-downloads.py` проверяет доступность сетевых источников программ перед подготовкой рабочей среды. Это служебная проверка, не самостоятельный шаблон Semaphore.

`initialize-openbao.py` остаётся внутренней точкой первоначальной установки и восстановления. В Semaphore отдельного шаблона для неё нет. Для человека штатные действия вынесены в PVE-команду `infra-manager repair` и `infra-manager recover`.

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

## Операторская и внутренние команды

Штатная точка входа человека остаётся на физическом PVE:

```text
infra-manager guests
infra-manager status [VMID]
infra-manager deploy VMID
infra-manager sync VMID
infra-manager repair [VMID]
infra-manager recover
```

Но `scripts/infra-manager/host/manager.py` теперь является только минимальной оболочкой. Для всех обычных операций она проверяет VMID управляющего LXC и передаёт аргументы через `pct exec` установленной команде `/usr/local/sbin/infra-manager` внутри 910. Реальная операторская логика находится в `infra_manager/operator.py`.

`deploy/sync/repair` из PVE не создают задание Semaphore через API. Перед прямым `deploy` оператор внутри 910 сам обновляет постоянную Git-копию под исключительной блокировкой и создаёт временный снимок внутри `infra-runtime`; `sync/repair` создают снимок текущей копии под разделяемой блокировкой. После создания снимка длительная операция идёт уже без блокировки проекта. Шаблоны Semaphore вызывают ту же точку входа из своей рабочей копии задания. Оба пути сходятся в `run_guest_operation()` и используют общий каталог `/var/lib/infra-manager/locks`; изменяющие операции блокируются по VMID.

Доверенный вызов с PVE помечается `--trusted-pve`; только в этом режиме итоговый status может вывести операторские пароли в текущий root-терминал. Semaphore, Homepage и внутренние проверки этот режим не используют.

`repair` без VMID может запустить остановленный управляющий LXC, но Docker/OpenBao/активация runtime выполняются уже операторским слоем внутри 910. `recover` является исключением: PVE-оболочка передаёт его напрямую `infra-manager-recovery`, чтобы восстановление не зависело от работоспособности 910.

Внутри 910 также остаётся техническая `infra-manager-status`. Отдельные установленные команды `infra-manager-pve-access-check` и `infra-manager-pve-lifecycle-test` удалены.

`infra-manager-activate-runtime` и `infra-manager-openbao-startup-unseal` являются внутренними командами автоматизации 910. На PVE остаются только тонкая оболочка, `infra-manager-recovery`, `infra-manager-openbao-unseal`, пакет его поддержки и PVE-only данные.

Тонкая PVE-оболочка устанавливается одним путём: роль Ansible сначала устанавливает `/usr/local/sbin/infra-manager` внутри управляющего гостя, затем отдельной внутренней командой `operator-wrapper-install` обновляет оболочку на PVE. `guest_deploy`, инициализация OpenBao и подготовка recovery не устанавливают её напрямую.

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

Здесь находятся проверки Python-модулей, инфраструктурных контрактов, первоначальной установки и безопасности. Они запускаются локально либо в GitHub Actions; тесты, которым требуется специальная работающая среда, подготавливают её отдельно.

- **Гости и SSH:** `test-guest-deploy.py`, `test-guest-operations.py`, `test-guest-resolver.py`, `test-guest-ssh.py`, `test-guest-status.py`, `test-access-contract.py`.
- **Управляющая программа:** `test-infra-manager-python.py`, `test-python-command.py`, `test-manager-host.py`, `test-status.py`, `test-portal.py`.
- **Первоначальная установка и восстановление:** `test-bootstrap-host.py`, `test-bootstrap-architecture.sh`, `test-recovery-host.py`, `test-runtime-downloads.py`.
- **OpenBao:** `test-openbao.py`, `test-openbao-live.py`, `test-openbao-ssh.py`, `test-openbao-otp-client.py`, `test-openbao-otp-verify.py`.
- **PVE, OpenTofu и шаблоны:** `test-guest-storage.py`, `test-pve-host.py`, `test-pve-lifecycle.py`, `test-opentofu-input.py`, `test-template.py`.
- **Описание служб:** `test-ai-control-contract.py`, `test-network-gateway-contract.py`.
- **Контрактные проверки 910:** `test-infra-manager-guest-contract.sh`, `test-infra-manager-openbao-contract.sh`, `test-infra-manager-ssh-contract.sh`, `test-infra-manager-tooling-contract.sh` и общий запускатель `test-infra-manager-contract.sh`.

`test-guest-ssh.py` отдельно проверяет единый SSH-сеанс, отсутствие зависимости от старого `known_hosts` и границы первоначального доступа. `test-openbao-live.py` проверяет OpenBao в подготовленной испытательной среде; `scripts/acceptance/verify-ssh-access.py` предназначен для сквозной проверки реальных разрешённых и запрещённых SSH OTP-связей.

## Правило размещения нового кода

- код жизненного цикла 910 → `scripts/infra-manager/`;
- задания Semaphore → `scripts/infra-manager/jobs/`;
- тонкая операторская оболочка PVE → `scripts/infra-manager/host/manager.py`;
- операторская логика 910 → `scripts/infra-manager/infra_manager/operator.py`;
- внутренние команды 910 → `scripts/infra-manager/commands/`;
- общая логика конфигурации гостей → `scripts/guests/`;
- проверки → `scripts/tests/`;
- общая проверка всего репозитория → `scripts/validate_repo.py`.

Не создавать новый параллельный контур развёртывания на PVE.
