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
        ├── render-template-cloud-init.py
        ├── lib/
        └── tests/
```

## `guest_config.py`

Общий модуль преобразования исходной конфигурации гостевой системы в итоговое требуемое состояние.

Его переиспользуют validator, `sync-management-keys` и `deploy-guest`, чтобы в одном месте существовали:

- правила merge `defaults + profile + guest`;
- VMID-адресация;
- разрешение Guest Bootstrap v1;
- канонический порядок capabilities;
- зависимости capabilities.

Основная спецификация данных: [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md).

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

Первый фиксирует контракт Guest Bootstrap v1: явный набор capabilities, порядок, зависимости и требование `start_after_deploy=true`. Второй проверяет безопасную работу management-key registry, managed block `authorized_keys` и guest public catalog. Третий проверяет PLAN/safety helpers, ownership/tags, disk rules и CLI `deploy-guest` без подключения к живому PVE.

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
- использует PVE API token `deployer@pve!host-deploy` только для read-only discovery объектов с tag `management-ssh`;
- для manifest-backed гостей использует общий `guest_config.py`, для внешне созданных tagged гостей — каноническую VMID-адресацию;
- проверяет Debian и root SSH через deployer identity;
- не принимает неизвестные SSH host keys: первичное доверие только что созданному ожидаемому объекту выполняет `deploy-guest`, а bulk sync требует уже сохранённый persistent host key;
- синхронизирует `/etc/proxmox-guest/public-keys/` и только managed block `/root/.ssh/authorized_keys`;
- не запускает offline-гостей ради sync;
- после APPLY повторно проверяет фактическое состояние.

Основная спецификация: [`../docs/28-management-ssh-keys.md`](../docs/28-management-ssh-keys.md).

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
60-template-contract.sh
61-template-source.sh
62-template-build.sh
63-template-smoke.sh
64-cloud-init-status.sh
70-tooling.sh
71-sync-management-keys-tooling.sh
```

Имена файлов являются частью структуры кода. Нумерация задаёт порядок и группировку этапов настройки PVE.

PVE Configuration также подготавливает runtime для deploy-контура: Python/YAML/JSON Schema, API credentials, PVE guest SSH identity, canonical management public-key registry, отдельный guest `known_hosts` и стабильную команду `sync-management-keys`.

## Генератор Cloud-Init шаблона

```text
scripts/pve/setup/render-template-cloud-init.py
```

Это основной генератор Cloud-Init для Debian-шаблона `9000`. Его требования описаны в [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## Тесты PVE Configuration

```text
scripts/pve/setup/tests/
```

Здесь находятся проверки контракта и безопасности настройки PVE и сборки шаблона. При изменении поведения тесты должны меняться вместе с кодом.

## `pve/deploy-guest.py`

Рабочий PLAN/APPLY runtime одной управляемой VM/LXC с `profile`. По умолчанию команда строит read-only PLAN, а изменения разрешаются только с `--apply`. Основной источник требований:

- [`../docs/31-deploy-guest.md`](../docs/31-deploy-guest.md) — полный PLAN/APPLY, Proxmox API, VM/LXC, SSH trust, Guest Bootstrap v1, ошибки и финальная проверка.

Связанные документы:

- [`../docs/26-deploy-guest-and-agent-access.md`](../docs/26-deploy-guest-and-agent-access.md) — запуск и разделение учётных записей;
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — source/effective state и `bootstrap.capabilities`;
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — точный контракт Guest Bootstrap и граница с Ansible.

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
- [`../docs/28-management-ssh-keys.md`](../docs/28-management-ssh-keys.md) — management SSH keys и `sync-management-keys`.
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — модель данных гостевых систем.
- [`../docs/31-deploy-guest.md`](../docs/31-deploy-guest.md) — спецификация `deploy-guest`.
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — Bootstrap и Ansible handoff.
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — спецификация сборки шаблона.
