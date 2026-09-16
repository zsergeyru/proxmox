# Версии и воспроизводимость Public Bootstrap / PVE Configuration / template

## Статус

Архитектура остаётся простой: project-owned contracts версионируются явно, внешние stable-компоненты не pin'ятся без практической необходимости.

Текущие версии:

```text
Public Bootstrap:        PUBLIC_BOOTSTRAP_VERSION=8
PVE Configuration:      PVE_CONFIGURATION_VERSION=17
Debian VM template:     Template-Version 6
```

## Public Bootstrap

Канонический entrypoint:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

`PUBLIC_BOOTSTRAP_VERSION` повышается, когда меняется поведение публичной точки входа: first-run bootstrap, resume, handoff, permanent refresh, marker contract, orchestration locking или передаваемые параметры.

Public Bootstrap не pin'ит private `main` между разными запусками: в начале каждого штатного запуска получает текущую ветку `main`. Но после выбора revision конкретный запуск фиксирует её и передаёт SHA в PVE Configuration. Одна PVE Configuration не должна смешивать код из разных Git revisions.

Public Bootstrap и PVE Configuration используют одну lock:

```text
/run/lock/proxmox-orchestration.lock
```

Lock удерживается до завершения configuration run. Поэтому другой Bootstrap или прямой `configure-pve.sh` не может одновременно переключить canonical checkout или изменить host configuration.

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
- rerun/locking/safety checks.

Перед `source lib/*.sh` entrypoint проверяет сам исполняемый checkout:

```text
Git worktree существует
→ HEAD = PVE_CONFIGURATION_SOURCE_REVISION
→ tracked/staged/untracked/ignored drift отсутствует
→ только затем source модулей
```

Это означает, что SHA в state/log действительно соответствует исполняемому коду. Dirty checkout не очищается автоматически: run останавливается, чтобы не потерять локальные данные и не скрыть drift.

Canonical checkout `/var/lib/proxmox-deployer/repo` также должен быть чистым до автоматического `fetch/reset`. Если обнаружен локальный drift, Bootstrap/PVE Configuration останавливаются вместо destructive overwrite.

Revision текущего run известна до первого host-side изменения и используется в `running/failed/interrupted` state. После sync canonical checkout обязан иметь тот же SHA. Фактически применённая revision записывается в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

State-файлы записываются атомарно через temporary file + `mv`:

```text
state.json
last-run.json
version
```

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
→ canonical Cloud-Init renderer

62-template-build.sh
→ создать VM builder
→ provisioning/QGA/Cloud-Init
→ verification reboot
→ fail-closed guest cleanup
→ standard Proxmox Cloud-Init
→ qm template
→ protection=1
→ final contract check
```

Canonical renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Он используется и runtime, и CI. Поэтому CI не поддерживает вторую независимую реализацию render-логики.

Guest-side assets:

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

`Template-Version` — версия guest/template contract, а не pin внешнего Debian build. Усиление host-side validation/locking/CI не повышает Template-Version, пока содержимое guest contract остаётся v6.

PVE Configuration принимает существующий template только если полный host-visible contract соответствует текущему baseline. Проверяются не только name/Cloud-Init markers, но и CPU/RAM, SCSI controller, system disk/storage/flags/minimum size, Cloud-Init drive, VirtIO network/bridge, boot order, QGA, console и protection.

Guest cleanup fail-closed: до `qm template` подтверждается отсутствие generic `debian`, machine-id, SSH host keys, `/root/.ssh`, build state и builder scripts. Незавершённая VM-сборщик сохраняется для диагностики.

## LXC appliance

PVE Configuration сначала обнаруживает уже загруженный Debian 13 LXC template, затем пытается обновить `pveam` catalog.

```text
pveam update успешен
→ использовать newest Debian 13
→ скачать при необходимости
→ удалить старые Debian 13 template caches

pveam update временно недоступен + локальный Debian 13 есть
→ WARNING
→ использовать локальный template

pveam update недоступен + локального Debian 13 нет
→ STOP
```

Таким образом кратковременная недоступность appliance catalog не ломает rerun уже подготовленного host.

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
exact-version pin каждого Debian package
автоматический hold всех установленных версий
```

Такие механизмы добавляются только для конкретного компонента, если появляется практическая причина.

## CI

Repository checks должны проверять как минимум:

- `scripts/validate_repo.py`;
- Python compilation;
- `bash -n`;
- ShellCheck с учётом sourced-module architecture;
- production Cloud-Init renderer;
- `cloud-init schema` итогового документа;
- template contract unit tests;
- негативный тест malformed guest directory;
- whitespace errors;
- отсутствие legacy `scripts/pve/create-template.sh`.

CI не заменяет реальный PVE integration test. После существенного изменения template guest assets по-прежнему требуется clean build + Full Clone smoke-test по принятой policy.

## Обязательные правила воспроизводимости

Project scripts должны:

- проверять скачиваемые artefacts checksum/signature механизмами, если они доступны;
- не хранить secrets в Git;
- явно проверять результат установки;
- фиксировать source revision с начала run;
- не смешивать несколько Git revisions внутри одного configuration run;
- не исполнять dirty worktree под именем чистого SHA;
- не делать молчаливый destructive overwrite локального drift;
- использовать общий orchestration lock для операций над canonical runtime;
- версионировать собственные contracts;
- соблюдать security boundaries;
- останавливать выполнение при неоднозначном или несовместимом state.

## Связанные документы

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`31-bootstrap.md`](31-bootstrap.md);
- [`../templates/debian13/README.md`](../templates/debian13/README.md);
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

Главный принцип:

> Не фиксировать внешние версии без практической необходимости, но явно версионировать собственные contracts, сохранять provenance и выполнять каждый configuration run из одного чистого checkout одной точно определённой private revision.
