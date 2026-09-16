# Версии и воспроизводимость Public Bootstrap / PVE Configuration / template

## Статус

Архитектура остаётся простой: project-owned contracts версионируются явно, внешние stable-компоненты не pin'ятся без практической необходимости.

Текущие версии:

```text
Public Bootstrap:        PUBLIC_BOOTSTRAP_VERSION=7
PVE Configuration:      PVE_CONFIGURATION_VERSION=16
Debian VM template:     Template-Version 6
```

## Public Bootstrap

Канонический entrypoint:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

`PUBLIC_BOOTSTRAP_VERSION` повышается, когда меняется поведение публичной точки входа: first-run bootstrap, resume, handoff, permanent refresh, marker contract или передаваемые параметры.

Public Bootstrap не pin'ит private `main` заранее между разными запусками: в начале каждого штатного запуска получает текущую ветку `main`. Но после выбора revision конкретный запуск фиксирует её и передаёт SHA в PVE Configuration. Одна PVE Configuration не должна смешивать код из разных Git revisions.

Если первый запуск прервался после создания permanent runtime, повторная та же команда продолжает bootstrap с существующими credentials и canonical checkout, а не требует удаления runtime.

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
- template pipeline requirements;
- state/reporting schema;
- поведение rerun/safety checks.

Фактически применённая private revision записывается в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

При handoff Public Bootstrap передаёт `PVE_CONFIGURATION_SOURCE_REVISION`. Canonical checkout должен совпасть именно с этой revision. Если ветка `main` успела измениться во время первого clone/fetch, PVE Configuration останавливается вместо смешивания уже загруженных модулей с более новым template/tooling кодом.

## Debian VM template

Создание VMID `9000` является встроенной частью PVE Configuration, а не отдельным самостоятельным host-side скриптом.

Host-side pipeline:

```text
60-template-contract.sh
→ состояние VMID 9000 + полный host-visible contract

61-template-source.sh
→ capacity/source checks
→ Debian cloud image + SHA512SUMS
→ строгая SHA-512 verification
→ сборка temporary Cloud-Init snippet из versioned assets

62-template-build.sh
→ создать VM builder
→ provisioning/QGA/Cloud-Init
→ verification reboot
→ guest cleanup
→ standard Proxmox Cloud-Init
→ qm template
→ protection=1
→ final contract check
```

Guest-side assets находятся в:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
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

`Template-Version` — версия нашего guest/template contract, а не pin внешнего Debian build. Перенос orchestration из отдельного `create-template.sh` в модули PVE Configuration сам по себе не требует повышения Template-Version, пока итоговое содержимое и contract гостя не изменены.

PVE Configuration принимает существующий template только если полный host-visible contract соответствует текущей версии. Несовместимый VMID `9000` автоматически не удаляется и не заменяется. Незавершённая VM-сборщик также сохраняется для диагностики и распознаётся как отдельное состояние.

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
- не смешивать несколько Git revisions внутри одного configuration run;
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

> Не фиксировать внешние версии без практической необходимости, но явно версионировать собственные contracts, сохранять provenance и выполнять каждый configuration run из одной точно определённой private revision.
