# Guests

`guests/` хранит desired state и локальную документацию VM/LXC. Этот README объясняет структуру каталога; точный manifest contract находится в [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md).

## Структура

```text
guests/
├── defaults.yaml
├── README.md
└── <VMID>-<name>/
    ├── guest.yaml
    ├── README.md
    ├── decisions/
    ├── STATUS.md
    └── rootfs/
```

Не каждый guest обязан иметь все дополнительные каталоги/файлы. Создаются только те, которые реально нужны.

## Роли файлов

| Файл/каталог | Назначение |
|---|---|
| `defaults.yaml` | Общие deploy defaults и profiles для управляемых гостей |
| `<guest>/guest.yaml` | Индивидуальный desired state конкретной VM/LXC |
| `<guest>/README.md` | Назначение и эксплуатационные особенности гостя |
| `<guest>/decisions/` | ADR — устойчивые решения, относящиеся только к этому гостю |
| `<guest>/STATUS.md` | Временный observed status/drift, если он действительно нужен |
| `<guest>/rootfs/` | Управляемые файлы, которые должны попадать внутрь guest OS |

`README.md`, ADR и `STATUS.md` не заменяют `guest.yaml` как deploy desired state.

## Источник истины

Для deployable VM/LXC effective desired state строится из:

```text
guests/defaults.yaml
+ выбранный profile
+ guests/<guest>/guest.yaml
+ детерминированные project rules
→ effective desired state
```

Точные schema, merge semantics, management IP resolution, network override rules, VM/LXC source contract и validator behavior описаны только в [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) и `../schemas/`.

Поэтому в этом README не фиксируются номера schema, конкретные subnet/gateway, Template-Version или списки обязательных полей — они быстро устаревают и принадлежат канонической спецификации.

## Что хранить в `guest.yaml`

Только индивидуальные параметры гостя и осознанные overrides.

Общие значения должны жить в `defaults.yaml`/profiles. Не следует копировать в каждый manifest общую сеть, management user, storage или другие defaults только ради наглядности.

Если объект известен проекту, но ещё не должен управляться универсальным deployer, используется предусмотренное manifest contract состояние `deployable: false`, а не отдельная параллельная система описания.

## Документация конкретного гостя

`README.md` гостя полезен для информации, которую не стоит кодировать в manifest, например:

- назначение VM/LXC;
- какие сервисы внутри ожидаются;
- эксплуатационные особенности;
- ссылки на внешний проект/репозиторий;
- необычные recovery или migration notes.

Устойчивые архитектурные решения лучше оформлять ADR в `decisions/`, чтобы было видно контекст и причину решения.

`STATUS.md` используется только для временного состояния или drift. После устранения drift постоянное правило должно перейти в manifest, policy или ADR.

## Managed files

`rootfs/` содержит только файлы, которыми проект действительно управляет как частью guest configuration.

Persistent application data, runtime state, backups и secrets не следует складывать в `rootfs/`.

Общая Linux filesystem policy: [`../docs/34-linux-filesystem-layout.md`](../docs/34-linux-filesystem-layout.md).

## Проверка

После изменения manifests выполнить:

```bash
python scripts/validate_repo.py
```

Validator и deployer должны использовать общий resolver `scripts/guest_config.py`, чтобы merge/addressing logic не расходилась между разными инструментами.

## Связанные документы

- [`../docs/11-vmid-plan.md`](../docs/11-vmid-plan.md) — VMID/CTID plan.
- [`../docs/23-security.md`](../docs/23-security.md) — SSH identities и security.
- [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md) — канонический manifest specification.
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — initial access/bootstrap/provisioning.
- [`../templates/debian13/README.md`](../templates/debian13/README.md) — паспорт Debian VM template `9000`.
