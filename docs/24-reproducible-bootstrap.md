# Версии и воспроизводимость Public Bootstrap / PVE Configuration / template

## Статус

Архитектура остаётся простой: project-owned contracts версионируются явно, внешние stable-компоненты не pin'ятся без практической необходимости.

Текущие версии:

```text
Public Bootstrap:        PUBLIC_BOOTSTRAP_VERSION=11
PVE Configuration:      PVE_CONFIGURATION_VERSION=21
Debian VM template:     Template-Version 7
```

## Public Bootstrap

Канонический entrypoint:

```text
zsergeyru/proxmox-bootstrap/bootstrap-pve.sh
```

`PUBLIC_BOOTSTRAP_VERSION` повышается, когда меняется поведение публичной точки входа: first-run bootstrap, resume, handoff, permanent refresh, marker contract, orchestration locking, source trust boundary или передаваемые параметры.

Public Bootstrap v11 дополнительно принимает `--smoke-test-template` и без изменения смысла передаёт его private PVE Configuration. Для уже существующего template штатная команда выглядит так:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --smoke-test-template
```

Public Bootstrap не pin'ит private `main` между разными запусками: в начале каждого штатного запуска получает текущую ветку `main`. Но после выбора revision конкретный запуск фиксирует её и передаёт SHA в PVE Configuration. Одна PVE Configuration не должна смешивать код из разных Git revisions.

Public Bootstrap и PVE Configuration используют одну lock:

```text
/run/lock/proxmox-orchestration.lock
```

Lock удерживается до завершения configuration run. Поэтому другой Bootstrap или прямой `configure-pve.sh` не может одновременно переключить canonical checkout или изменить host configuration.

Если первый запуск прервался после создания permanent runtime, повторная та же команда продолжает bootstrap с существующими credentials и canonical checkout, а не требует удаления runtime.

Temporary checkout `/var/lib/proxmox-bootstrap/private-repo` является disposable. При resume он обновляется на текущий `FETCH_HEAD` и очищается `git clean -ffdx`, включая ignored cache/build artifacts. Permanent checkout остаётся fail-closed: local drift там не удаляется автоматически.

## Root trust boundary

Canonical source — это root-trusted host configuration, а не mutable workspace `pvedeploy`.

Contract:

```text
/var/lib/proxmox-deployer
→ root:pvedeploy 0750

/var/lib/proxmox-deployer/repo
→ root-owned files/directories
→ group/other write отсутствует

/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
→ root:root 0600

/etc/proxmox-deployer/ssh/config
→ root:root 0600
```

Родитель canonical repo также не writable для `pvedeploy`, поэтому ограниченный runtime user не может заменить root-owned каталог целиком.

Public Bootstrap v10+ переводит существующую старую `pvedeploy`-owned установку в эту модель до handoff. Git refresh permanent source после этого выполняется от root. `pvedeploy` сохраняет read access к source и свои deployment credentials/runtime, но не Git credential и не write access к repo/.git.

## PVE Configuration

Канонический entrypoint:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
```

`PVE_CONFIGURATION_VERSION` повышается, когда меняется contract host configuration, например runtime layout, credentials model, filesystem/source trust boundary, PVE roles/ACL, storage requirements, template pipeline, state schema или safety checks. Version 21 добавляет штатный Full Clone smoke lifecycle и persistent smoke state.

Перед `source lib/*.sh` entrypoint проверяет сам исполняемый checkout:

```text
процесс запущен от root
→ Git worktree существует
→ regular files/directories root-owned
→ group/other write отсутствует
→ HEAD = PVE_CONFIGURATION_SOURCE_REVISION
→ tracked/staged/untracked/ignored drift отсутствует
→ только затем source модулей
```

Canonical checkout `/var/lib/proxmox-deployer/repo` обязан одновременно соответствовать source SHA, clean-state и root ownership contract. Если обнаружен local drift, Bootstrap/PVE Configuration останавливаются вместо destructive overwrite.

Revision текущего run известна до первого host-side изменения и используется в `running/failed/interrupted` state. После sync canonical checkout обязан иметь тот же SHA. Фактически применённая revision записывается в:

```text
/var/lib/proxmox-deployer/state/last-revision
```

State-файлы записываются атомарно через temporary file + `mv`:

```text
state.json
last-run.json
version
template-smoke.json
```

`template-smoke.json` содержит `pending` или `passed`. `pending` переживает interruption и делает smoke-test обязательным при следующем обычном run.

Canonical GitHub SSH transport использует strict host checking, `BatchMode`, bounded connect timeout и server-alive policy. HTTP/HTTPS sanity-checks также имеют connection и total timeout.

## API token durability

Новый PVE API token имеет одноразовый secret, поэтому создание token и сохранение secret рассматриваются как одна операция:

```text
pveum user token add
→ отметить token как pending текущего run
→ разобрать one-time secret
→ записать secret во временный файл
→ выставить owner/mode
→ atomic mv в canonical secret file
→ снять pending marker
```

Parsing/write failure и обычные error/INT/TERM/EXIT paths пытаются удалить только новый pending token текущего run. Уже существующие tokens не удаляются и не ротируются автоматически. `SIGKILL`/авария питания не могут сделать межсистемную операцию полностью атомарной.

## Debian VM template и smoke-test

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
→ ASCII stage telemetry через QGA
→ host-side русские подписи этапов
→ verification reboot
→ fail-closed guest cleanup
→ standard Proxmox Cloud-Init
→ smoke state=pending
→ qm template
→ protection=1
→ final contract check

63-template-smoke.sh
→ Full Clone 9000 → 9099
→ scsi0 16G → 20G до первого boot
→ Cloud-Init root + pve_guest SSH key + DHCP
→ QGA/Cloud-Init/kernel/SSH/machine-id/SSH-host-key/rootfs checks
→ reboot + повторная проверка
→ SUCCESS: shutdown + destroy 9099 + state=passed
→ ERROR/interrupt: сохранить 9099 + state остаётся pending
```

Canonical renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Guest-side assets:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

Текущая guest model остаётся `Template-Version 7`; smoke lifecycle меняет host-side configuration contract, поэтому поднимается PVE Configuration, а не Template-Version.

Фактически использованный image и SHA-512 записываются в `/etc/vm-template-info`, а Proxmox description содержит `template-version=7`.

PVE Configuration принимает существующий template только если полный host-visible contract соответствует baseline. Проверяются CPU/RAM, SCSI controller, system disk/storage/flags/minimum size, exact Cloud-Init volume `local-lvm:vm-9000-cloudinit` с `media=cdrom`, VirtIO network/bridge, boot order, QGA, console и protection.

`template_guest_exec` нормализует plain CLI output и structured QGA output. Incomplete structured result (`pid` без завершения, `exited=0`), signal или ненулевой exitcode считается ошибкой. Smoke module повторно использует этот же executor для VMID `9099`, а не реализует второй QGA parser.

Builder stage-файл `/var/lib/template-build/bootstrap-status` содержит только ASCII identifiers. Localized русские подписи формируются на PVE host и не проходят через QGA byte/string transport.

Guest cleanup fail-closed: до `qm template` подтверждается отсутствие generic `debian`, machine-id, SSH host keys, `/root/.ssh`, build state и builder scripts. Smoke-test проверяет обратный lifecycle: Full Clone получает новый machine-id, новые SSH host keys, injected root SSH key и расширенный filesystem.

Smoke trigger policy:

```text
новый template создан текущим run
→ AUTO smoke

--smoke-test-template
→ FORCE smoke существующего template

template-smoke.json status=pending
→ RESUME smoke при обычном rerun
```

Safety policy для `9099`:

```text
9099 свободен
→ разрешён smoke clone

9099 = project smoke residue
→ STOP, оставить для диагностики

9099 = обычная VM или LXC
→ STOP, ничего не менять и не удалять

полный SUCCESS
→ удалить 9099 только после всех проверок
```

## LXC appliance

PVE Configuration сначала обнаруживает уже загруженный Debian 13 LXC template, затем пытается обновить `pveam` catalog. При временной недоступности catalog локальный Debian 13 можно использовать повторно; при отсутствии и catalog, и local template выполняется STOP.

## Внешние версии

Для внешних stable components базовый принцип:

```text
обычная установка
→ актуальная stable версия из выбранного trusted channel
→ штатная checksum/signature verification, если доступна
→ явная проверка результата
```

Не являются обязательными общепроектными механизмами `snapshot.debian.org`, exact-version pin каждого Debian package и автоматический hold всех установленных версий. Они добавляются только для конкретного компонента при практической необходимости.

## CI

Repository checks должны проверять как минимум:

- `scripts/validate_repo.py`;
- Python compilation;
- `bash -n`;
- ShellCheck с учётом sourced-module architecture;
- production Cloud-Init renderer и `cloud-init schema`;
- отсутствие embedded `guest-status` helper;
- точный ASCII stage-code contract builder-а;
- template contract unit tests;
- guest-exec result/timeout и local stage-label unit tests;
- template smoke safety/state unit tests;
- API token rollback unit tests;
- source trust boundary test;
- malformed guest directory negative test;
- whitespace errors;
- отсутствие legacy `scripts/pve/create-template.sh`.

CI не может эмулировать реальный Proxmox/QEMU runtime. Реальный Full Clone smoke теперь выполняется самой PVE Configuration: автоматически после новой сборки либо явно через `--smoke-test-template` для существующего template.

## Обязательные правила воспроизводимости

Project scripts должны:

- проверять скачиваемые artefacts checksum/signature механизмами, если они доступны;
- не хранить secrets в Git;
- явно проверять результат установки;
- фиксировать source revision с начала run и не смешивать revisions;
- не исполнять dirty или non-root-trusted source;
- держать canonical Git credential root-only;
- не делать молчаливый destructive overwrite local drift;
- использовать общий orchestration lock;
- сохранять одноразовые credentials атомарно и откатывать только вновь созданные текущим run credentials;
- для QGA progress передавать ASCII identifiers, localized presentation формировать host-side;
- оставлять failed smoke VM для диагностики и удалять её только после полного success;
- сохранять smoke `pending` до подтверждённого real Full Clone lifecycle;
- версионировать собственные contracts;
- останавливать выполнение при неоднозначном или несовместимом state.

## Связанные документы

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`31-bootstrap.md`](31-bootstrap.md);
- [`../templates/debian13/README.md`](../templates/debian13/README.md);
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

Главный принцип:

> Не фиксировать внешние версии без практической необходимости, но явно версионировать собственные contracts, сохранять provenance и выполнять каждый configuration run из одного чистого root-trusted checkout одной точно определённой private revision; новый template считается runtime-проверенным только после успешного Full Clone smoke-test.
