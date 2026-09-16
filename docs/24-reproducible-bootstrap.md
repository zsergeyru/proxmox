# Версии и воспроизводимость Public Bootstrap / PVE Configuration / template

## Статус

Архитектура bootstrap/deploy сохраняется простой: мы явно версионируем собственные project contracts, но не фиксируем каждую внешнюю stable-версию без практической необходимости.

## Template

Активный builder:

```text
zsergeyru/proxmox/scripts/pve/create-template.sh
```

Текущая модель:

```text
Template-Version 6
официальный Debian 13 trixie/latest
→ SHA-512 verification
→ обычные Debian repositories
→ apt update/full-upgrade
→ root-only key-based SSH policy
→ сборка template
```

Фактически использованный image и SHA-512 записываются в `/etc/vm-template-info`, а Proxmox description содержит:

```text
template-version=6
```

Это позволяет PVE Configuration отличить совместимый root-only template от старых baseline.

## Совместимость template

`Template-Version` — версия project contract, а не pin внешнего Debian build.

```text
v4
→ management user ops

v6
→ management user root
→ root password locked
→ root SSH only by public key
```

PVE Configuration не мигрирует и не перезаписывает защищённый старый `9000` автоматически. Несовместимая версия вызывает STOP и отдельную осознанную пересборку.

## Версии project-owned компонентов

Public entrypoint:

```text
PUBLIC_BOOTSTRAP_VERSION=5
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

Private host configuration:

```text
PVE_CONFIGURATION_VERSION=13
scripts/pve/setup/configure-pve.sh
```

Версии повышаются, когда меняется контракт или поведение компонента. Они не означают номер последовательного «этапа».

Legacy `STAGE0_VERSION` и `BOOTSTRAP_VERSION` больше не являются каноническими version names; старые paths/marker поддерживаются только как compatibility layer.

Для внешних stable components принцип:

```text
обычная установка
→ актуальные stable версии
→ штатная checksum/signature verification
→ проверка результата
```

Если конкретному компоненту позже потребуется pin/recovery baseline, он добавляется точечно.

## Что не является обязательным

```text
BOOTSTRAP_MODE=normal/recovery
общий bootstrap-versions.env
immutable BOOTSTRAP_REF для каждого запуска
Docker exact-version + apt-mark hold
Hermes exact commit pin
Proximo exact package pin
Debian APT snapshot
```

Эти механизмы могут использоваться только при появлении практической необходимости.

## Обязательные правила

Project scripts должны:

- проверять скачиваемые artefacts checksum/signature механизмами, если они доступны;
- не хранить secrets в Git;
- явно проверять результат установки;
- не делать молчаливый destructive overwrite;
- фиксировать достаточную provenance-информацию;
- проверять compatibility project contracts, например Template-Version 6;
- соблюдать security boundaries;
- сохранять совместимость старых entrypoint только через тонкие wrappers, а не дублировать реализацию.

## Связанные документы

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`31-bootstrap.md`](31-bootstrap.md);
- [`../templates/debian13/README.md`](../templates/debian13/README.md);
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## Главный принцип

> Не фиксировать внешние версии без практической необходимости, но явно версионировать собственные contracts, когда изменение влияет на совместимость и безопасность deploy.
