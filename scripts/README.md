# Scripts

`scripts/` содержит исполняемый код проекта, который относится к инфраструктуре в целом, а не к одному конкретному guest.

Этот README — карта кода. Архитектурные правила и contracts должны жить в `docs/`, а не дублироваться здесь.

## Структура

```text
scripts/
├── guest_config.py
├── validate_repo.py
└── pve/
    └── setup/
        ├── configure-pve.sh
        ├── render-template-cloud-init.py
        ├── lib/
        └── tests/
```

## `guest_config.py`

Общий resolver source guest configuration → effective desired state.

Его должны переиспользовать validator и будущий guest deploy tooling, чтобы merge/addressing logic существовала в одном месте.

Канонический data contract: [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md).

## `validate_repo.py`

Repository validator для guest/defaults/schema и связанных project invariants.

Запуск:

```bash
python scripts/validate_repo.py
```

CI использует тот же repository contract; отдельную упрощённую реализацию правил для локальной проверки добавлять не следует.

## `pve/setup/`

Host-side PVE Configuration pipeline.

Основной entrypoint:

```text
scripts/pve/setup/configure-pve.sh
```

Он оркестрирует numbered modules из `lib/` и владеет общими lock/state/log/error semantics.

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

Нумерация задаёт порядок/группировку host configuration pipeline. Подробное назначение и safety contract определяются кодом, тестами и профильной документацией.

## Template renderer

```text
scripts/pve/setup/render-template-cloud-init.py
```

Это canonical renderer guest-side Cloud-Init для Debian template `9000`. Его contract описан в [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## Tests

```text
scripts/pve/setup/tests/
```

Здесь находятся unit/contract/safety tests PVE setup и template pipeline. При изменении поведения соответствующие тесты должны меняться вместе с кодом.

## `deploy-guest`

Канонический проектный contract будущего host-side guest deployment описывают:

- [`../docs/26-deploy-guest-and-agent-access.md`](../docs/26-deploy-guest-and-agent-access.md) — operational flow и разделение identities;
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — input/effective-state contract;
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — management readiness и provisioning boundary.

До появления соответствующего исполняемого файла README не должен описывать несуществующую внутреннюю структуру handlers как уже реализованную.

## Правила для кода

- Не дублировать business rules между validator, deployer и bootstrap scripts.
- Опасные операции должны иметь preflight и fail-safe/fail-closed поведение там, где это возможно.
- Повторный запуск должен быть безопасным либо побочный эффект должен быть явно задокументирован.
- Secrets не встраиваются в код и не выводятся в logs.
- Изменение contract должно сопровождаться изменением canonical документа и тестов, а не новым описанием в этом README.

## Связанные документы

- [`../docs/20-pve-initialization.md`](../docs/20-pve-initialization.md) — PVE initialization runbook.
- [`../docs/24-reproducible-bootstrap.md`](../docs/24-reproducible-bootstrap.md) — reproducibility/versioning.
- [`../docs/25-pve-access-control.md`](../docs/25-pve-access-control.md) — PVE roles/ACL.
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — guest data model.
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — template pipeline contract.
