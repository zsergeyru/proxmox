# Версии и воспроизводимость bootstrap/template

## Статус

Архитектура bootstrap/deploy сохраняется. Не используется прежнее усложнённое решение по жёсткой фиксации каждой внешней версии и общему recovery lock.

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

Не используются:

```text
pinned Debian cloud build
snapshot.debian.org
отдельный external version lock
общий recovery mode для builder
```

Фактически использованный image и его SHA-512 записываются в `/etc/vm-template-info`, а Proxmox description содержит marker:

```text
template-version=6
```

Это позволяет Stage 1 отличить совместимый root-only template от старых baseline.

## Совместимость template

`Template-Version` является не pin внешнего Debian build, а **версией нашего project contract**.

Поэтому переход v4 → v6 означает реальное изменение поведения:

```text
v4
→ management user ops

v6
→ management user root
→ root password locked
→ root SSH only by public key
```

Stage 1 не мигрирует и не перезаписывает защищённый старый `9000` автоматически. Несовместимая версия вызывает STOP и требует отдельной осознанной пересборки.

## Bootstrap/deploy versions

Для project-owned scripts версии повышаются, когда меняется их контракт/поведение. Текущий private bootstrap:

```text
BOOTSTRAP_VERSION=12
```

Для внешних stable components базовый принцип остаётся проще:

```text
обычная установка
→ актуальные stable версии
→ штатная checksum/signature verification
→ проверка результата
```

Если для конкретного компонента позже потребуется pin/recovery baseline, он добавляется точечно и обоснованно.

## Что отменено из предыдущего эксперимента

Не являются обязательными требованиями:

```text
BOOTSTRAP_MODE=normal/recovery
общий bootstrap-versions.env
immutable BOOTSTRAP_REF для каждого запуска
Docker exact-version + apt-mark hold
Hermes exact commit pin
Proximo exact package pin
Debian APT snapshot
```

Эти механизмы могут использоваться точечно, если появится практическая необходимость.

## Что остаётся обязательным

Даже при простой version policy scripts должны:

- проверять скачиваемые artefacts checksum/signature механизмами, если они доступны;
- не хранить secrets в Git;
- явно проверять результат установки;
- не делать молчаливый destructive overwrite;
- фиксировать достаточную provenance-информацию;
- проверять compatibility project contracts (например Template-Version 6);
- соблюдать security boundaries.

## Связанные документы

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`31-bootstrap.md`](31-bootstrap.md);
- [`../templates/debian13/README.md`](../templates/debian13/README.md);
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## Главный принцип

> Не фиксировать внешние версии без практической необходимости, но явно версионировать собственные контракты, когда изменение влияет на совместимость и безопасность deploy.
