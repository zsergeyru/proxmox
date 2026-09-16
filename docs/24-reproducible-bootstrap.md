# Версии и воспроизводимость Public Bootstrap / PVE Configuration / template

## Статус

Архитектура остаётся простой: project-owned contracts версионируются явно, внешние stable-компоненты не pin'ятся без практической необходимости.

Текущие версии:

```text
Public Bootstrap:        PUBLIC_BOOTSTRAP_VERSION=6
PVE Configuration:      PVE_CONFIGURATION_VERSION=14
Debian VM template:     Template-Version 6
```

## Public Bootstrap

Канонический entrypoint:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

`PUBLIC_BOOTSTRAP_VERSION` повышается, когда меняется поведение публичной точки входа: first-run bootstrap, handoff, permanent refresh, marker contract или передаваемые параметры.

Public Bootstrap не содержит private infrastructure policy и не pin'ит private revision заранее: при штатном запуске используется текущая ветка `main`, после чего фиксируется фактически применённый commit.

## PVE Configuration

Канонический entrypoint:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
```

`PVE_CONFIGURATION_VERSION` повышается, когда меняется contract host configuration, например:

- runtime layout;
- credentials model;
- PVE roles/ACL;
- storage requirements;
- template requirements;
- state/reporting schema;
- поведение rerun/safety checks.

Фактически применённая private revision записывается в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

## Debian VM template

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

`Template-Version` — версия нашего project contract, а не pin внешнего Debian build.

PVE Configuration принимает существующий template только если он соответствует текущему contract. Несовместимый protected VMID `9000` автоматически не удаляется и не заменяется.

## Внешние версии

Для внешних stable components базовый принцип:

```text
обычная установка
→ актуальная stable версия из выбранного trusted channel
→ штатная checksum/signature verification, если доступна
→ явная проверка результата
```

Не являются обязательными общепроектными механизмами:

```text
snapshot.debian.org для всех пакетов
общий immutable ref для каждого bootstrap run
exact-version pin каждого Debian package
общий recovery mode
автоматический hold всех установленных версий
```

Такие механизмы добавляются только для конкретного компонента, если появляется практическая причина.

## Обязательные правила воспроизводимости

Project scripts должны:

- проверять скачиваемые artefacts checksum/signature механизмами, если они доступны;
- не хранить secrets в Git;
- явно проверять результат установки;
- фиксировать фактически применённую private revision;
- версионировать собственные contracts;
- не делать молчаливый destructive overwrite;
- соблюдать security boundaries;
- останавливать выполнение при неоднозначном или несовместимом state.

## Связанные документы

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`31-bootstrap.md`](31-bootstrap.md);
- [`../templates/debian13/README.md`](../templates/debian13/README.md);
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

Главный принцип:

> Не фиксировать внешние версии без практической необходимости, но явно версионировать собственные contracts и сохранять provenance фактически применённого состояния.
