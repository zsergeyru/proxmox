# Скрипты

`scripts/` содержит исполняемый код проекта, который относится к инфраструктуре в целом, а не к одной конкретной гостевой системе.

Этот README — карта кода. Архитектурные правила и точные требования живут в `docs/`, а не дублируются здесь.

## Структура

```text
scripts/
├── guest_config.py
├── validate_repo.py
├── tests/
│   └── test-guest-bootstrap.py
└── pve/
    └── setup/
        ├── configure-pve.sh
        ├── render-template-cloud-init.py
        ├── lib/
        └── tests/
```

После реализации `deploy-guest` здесь также появятся его основной модуль и bootstrap-handlers; до появления реального кода README не изображает их как уже существующие.

## `guest_config.py`

Общий модуль преобразования исходной конфигурации гостевой системы в итоговое требуемое состояние.

Его переиспользуют validator и `deploy-guest`, чтобы в одном месте существовали:

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

CI и будущий `deploy-guest` используют этот же validator. Отдельную упрощённую реализацию validation для deployer создавать нельзя.

## `tests/`

Repo-level contract tests, которые не требуют живого PVE.

Сейчас здесь находится:

```text
scripts/tests/test-guest-bootstrap.py
```

Он фиксирует контракт Guest Bootstrap v1: явный набор capabilities, порядок, зависимости и требование `start_after_deploy=true`.

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
50-access.sh
60-template-contract.sh
61-template-source.sh
62-template-build.sh
63-template-smoke.sh
64-cloud-init-status.sh
70-tooling.sh
```

Имена файлов являются частью структуры кода. Нумерация задаёт порядок и группировку этапов настройки PVE.

PVE Configuration также подготавливает runtime, необходимый будущему deployer: Python/YAML/JSON Schema, API credentials, PVE guest SSH identity и отдельный guest `known_hosts`.

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

## `deploy-guest`

Основной источник требований к будущему `scripts/pve/deploy-guest.py`:

- [`../docs/31-deploy-guest.md`](../docs/31-deploy-guest.md) — полный PLAN/APPLY, Proxmox API, VM/LXC, SSH trust, Guest Bootstrap v1, ошибки и финальная проверка.

Связанные документы:

- [`../docs/26-deploy-guest-and-agent-access.md`](../docs/26-deploy-guest-and-agent-access.md) — запуск и разделение учётных записей;
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — source/effective state и `bootstrap.capabilities`;
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — точный контракт Guest Bootstrap и граница с Ansible.

## Правила для кода

- Не дублировать предметные правила между validator, resolver и deployer.
- PLAN без `--apply` должен быть строго read-only и для PVE, и для SSH/Bootstrap.
- Опасные и неоднозначные состояния должны приводить к `BLOCKED/STOP`, а не к догадке.
- Bootstrap capability должна быть идемпотентной: `check → apply if needed → verify`.
- Повторный запуск после частичной ошибки должен заново читать фактическое состояние.
- Секреты не встраиваются в код и не выводятся в журналы.
- Изменение требований сопровождается изменением основного документа и тестов.

## Связанные документы

- [`../docs/20-pve-initialization.md`](../docs/20-pve-initialization.md) — инструкция инициализации PVE.
- [`../docs/24-reproducible-bootstrap.md`](../docs/24-reproducible-bootstrap.md) — воспроизводимость и версионирование.
- [`../docs/25-pve-access-control.md`](../docs/25-pve-access-control.md) — роли и ACL PVE.
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — модель данных гостевых систем.
- [`../docs/31-deploy-guest.md`](../docs/31-deploy-guest.md) — спецификация `deploy-guest`.
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — Bootstrap и Ansible handoff.
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — спецификация сборки шаблона.
