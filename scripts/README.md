# Скрипты

`scripts/` содержит исполняемый код проекта, который относится к инфраструктуре в целом, а не к одной конкретной гостевой системе.

Этот README — карта кода. Архитектурные правила и точные требования живут в `docs/`, а не дублируются здесь.

## Структура

```text
scripts/
├── guest_config.py
├── validate_repo.py
├── tests/
│   ├── test-guest-bootstrap.py
│   ├── test-sync-management-keys.py
│   └── test-deploy-guest.py
└── pve/
    ├── deploy-guest.py
    ├── sync-management-keys.py
    └── setup/
        ├── configure-pve.sh
        ├── lib/
        └── tests/
```

## `infra-manager/`

Контур настройки специального LXC `910 infra-manager`.

Основные файлы:

```text
scripts/infra-manager/
├── setup.sh
├── infra_manager/
│   ├── __main__.py
│   ├── cli.py
│   ├── common.py
│   ├── setup.py
│   ├── semaphore.py
│   ├── pve.py
│   └── status.py
├── check-pve-access.sh
├── test-pve-lifecycle.sh
└── status.sh
```

`setup.sh` является минимальной оболочкой: при первом запуске обеспечивает наличие системного `python3` и передаёт управление команде `python3 -m infra_manager setup`. Основная подготовка Debian, Docker, постоянных каталогов, секретов, CA, OpenTofu input и `infra-runtime` выполняется в `infra_manager/setup.py`. Рабочая копия Python-пакета устанавливается в `/usr/local/lib/infra-manager`, поэтому команды из `/usr/local/sbin` не зависят от наличия bootstrap checkout.

`infra_manager/semaphore.py` напрямую вызывается из Python setup и создаёт или синхронизирует проект `Proxmox Infrastructure`, GitHub SSH key, репозиторий, Variable Group и задания Semaphore.

`check-pve-access.sh` — оболочка команды `python3 -m infra_manager pve-access-check`. Проверка HTTPS API и фактических privileges token `root@pam!infra-manager` находится в `infra_manager/pve.py`.

`status.sh` — оболочка Python status-проверки из `infra_manager/status.py` и устанавливается как:

```text
/usr/local/sbin/infra-manager-status
```

Дополнительно устанавливается:

```text
/usr/local/sbin/infra-manager-pve-access-check
```

Публичный bootstrap использует `infra-manager-status` при итоговой и повторной проверке `910`; внутренняя status-проверка вызывает проверку PVE ACL.

`test-pve-lifecycle.sh` устанавливается как:

```text
/usr/local/sbin/infra-manager-pve-lifecycle-test
```

Это только явный приёмочный тест. Он запускается исключительно с `--apply`, использует временный CTID `9098`, создаёт LXC сразу в `managed`, меняет RAM, запускает, останавливает и удаляет его. Bootstrap и CI автоматически этот тест не выполняют.

## `guest_config.py`

Общий модуль преобразования исходной конфигурации гостевой системы в итоговое требуемое состояние.

Его переиспользуют validator, `sync-management-keys` и `deploy-guest`, чтобы в одном месте существовали:

- правила merge `defaults + profile + guest`;
- VMID-адресация;
- нормализация списков `management` и `bootstrap`;
- VMID-адресация;
- проверка высокоуровневых profile features.

Основная спецификация данных: [`../docs/300-guests/310-guest-state.md`](../docs/300-guests/310-guest-state.md).

## `validate_repo.py`

Единый проверяющий скрипт репозитория для `guest.yaml`, `defaults.yaml`, schemas и связанных обязательных правил проекта.

```bash
python scripts/validate_repo.py
```

CI, `sync-management-keys` и `deploy-guest` используют этот же validator. Отдельную упрощённую реализацию validation для runtime создавать нельзя.

## `tests/`

Repo-level contract tests, которые не требуют живого PVE.

Сейчас здесь находятся:

```text
scripts/tests/test-guest-bootstrap.py
scripts/tests/test-sync-management-keys.py
scripts/tests/test-deploy-guest.py
```

Первый проверяет новый guest resolver и Bootstrap-список, включая правило `docker` для LXC только при profile feature `container-host`. Второй проверяет безопасную работу management-key registry, managed block `authorized_keys` и guest public catalog. Третий проверяет текущий контракт `deploy-guest` для effective state v10, включая VM/LXC, `pve_management` и преобразование `container-host`.

## `pve/sync-management-keys.py`

Runtime синхронизации инфраструктурных management public keys.

Штатная команда на PVE:

```bash
sync-management-keys
sync-management-keys --check
```

PVE Configuration устанавливает root-only wrapper `/usr/local/sbin/sync-management-keys`, который фиксирует trusted Git revision, берёт общую orchestration lock и запускает основной Python runtime от `pvedeploy`.

Runtime:

- валидирует canonical public-key registry до первой guest mutation;
- использует PVE API token `deployer@pve!host-deploy` только для read-only discovery фактически существующих VM/LXC;
- состав участников определяет из проектных `guest.yaml` с `profile`; перед изменением требует ownership `proxmox-deployer`, а внешние объекты без проектного manifest не включает в синхронизацию;
- проверяет Debian и root SSH через deployer identity;
- не принимает неизвестные SSH host keys: первичное доверие только что созданному ожидаемому объекту выполняет `deploy-guest`, а bulk sync требует уже сохранённый persistent host key;
- синхронизирует `/etc/proxmox-guest/public-keys/` и только managed block `/root/.ssh/authorized_keys`;
- не запускает offline-гостей ради sync;
- после APPLY повторно проверяет фактическое состояние.

Основная спецификация: [`../docs/700-security/720-ssh-access.md`](../docs/700-security/720-ssh-access.md).

## `pve/setup/`

Контур настройки PVE на стороне хоста.

Основная точка входа:

```text
scripts/pve/setup/configure-pve.sh
```

Она последовательно запускает пронумерованные модули из `lib/` и отвечает за общую блокировку, файлы состояния, журналы и обработку ошибок.

Текущие группы модулей:

```text
00-common.sh
10-preflight.sh
20-system.sh
30-storage.sh
40-runtime.sh
45-management-keys.sh
50-access.sh
70-tooling.sh
71-sync-management-keys-tooling.sh
```

Имена файлов являются частью структуры кода. Нумерация задаёт порядок и группировку этапов настройки PVE.

PVE Configuration также подготавливает runtime для deploy-контура: Python/YAML/JSON Schema, API credentials, PVE guest SSH identity, canonical management public-key registry, отдельный guest `known_hosts` и стабильную команду `sync-management-keys`.

## Шаблон VM 9000

PVE Configuration больше не строит базовый VM template. Единственный штатный путь — `Semaphore → scripts/infra-manager/build-template.sh 9000 → Packer`. Исходники и контракт Packer находятся в [`../packer/9000/`](../packer/9000/) и [`../packer/README.md`](../packer/README.md).

## Тесты PVE Configuration

```text
scripts/pve/setup/tests/
```

Здесь находятся проверки контракта и безопасности настройки PVE. Проверка Packer-шаблона выполняется отдельным infra-manager/Packer-контуром.

## `pve/deploy-guest.py`

Рабочий PLAN/APPLY runtime одной управляемой VM/LXC с `profile`. По умолчанию команда строит read-only PLAN, а изменения разрешаются только с `--apply`. Основной источник требований:

- [`../docs/300-guests/330-deploy-guest.md`](../docs/300-guests/330-deploy-guest.md) — полный PLAN/APPLY, Proxmox API, VM/LXC, SSH trust, Guest Bootstrap v1, ошибки и финальная проверка.

Связанные документы:

- [`../docs/700-security/710-pve-access.md`](../docs/700-security/710-pve-access.md) — запуск и разделение учётных записей;
- [`../docs/300-guests/310-guest-state.md`](../docs/300-guests/310-guest-state.md) — source/effective state, Management, Bootstrap и профили;
- [`../docs/300-guests/330-deploy-guest.md`](../docs/300-guests/330-deploy-guest.md) — точный контракт Guest Bootstrap и граница с Ansible.

## Правила для кода

- Не дублировать предметные правила между validator, resolver и runtime-компонентами.
- PLAN/check без apply должен быть строго read-only для PVE/SSH state.
- Опасные и неоднозначные состояния должны приводить к `BLOCKED/STOP`, а не к догадке.
- Guest mutation начинается только после полного preflight всех доступных участников соответствующей операции.
- Повторный запуск после частичной ошибки должен заново читать фактическое состояние.
- Секреты не встраиваются в код и не выводятся в журналы.
- Изменение требований сопровождается изменением основного документа и тестов.

## Связанные документы

- [`../docs/20-pve-initialization.md`](../docs/20-pve-initialization.md) — инструкция инициализации PVE.
- [`../docs/24-reproducible-bootstrap.md`](../docs/24-reproducible-bootstrap.md) — воспроизводимость и версионирование.
- [`../docs/25-pve-access-control.md`](../docs/25-pve-access-control.md) — роли и ACL PVE.
- [`../docs/700-security/720-ssh-access.md`](../docs/700-security/720-ssh-access.md) — управляющий SSH, реестр ключей и `sync-management-keys`.
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — модель данных гостевых систем.
- [`../docs/300-guests/330-deploy-guest.md`](../docs/300-guests/330-deploy-guest.md) — спецификация `deploy-guest`.
- [`../docs/300-guests/330-deploy-guest.md`](../docs/300-guests/330-deploy-guest.md) — Bootstrap и Ansible handoff.
- [`../packer/README.md`](../packer/README.md) — структура и правила Packer-шаблонов.
