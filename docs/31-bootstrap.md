# Bootstrap — текущее состояние

## Статус

Bootstrap PVE разделён на две исполняемые стадии.

```text
PUBLIC Stage 0
zsergeyru/proxmox-bootstrap/init-pve.sh

PRIVATE Stage 1
zsergeyru/proxmox/scripts/pve/bootstrap/init-pve.sh
```

Канонические архитектурные документы:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`25-pve-access-control.md`](25-pve-access-control.md);
- guest manifests и ADR соответствующих VM/LXC.

---

## Public Stage 0 — реализована

Публичный `zsergeyru/proxmox-bootstrap` содержит только zero-day loader.

Текущая версия public Stage 0:

```text
STAGE0_VERSION=2
```

Обычный сценарий выполняется за один интерактивный запуск:

```text
root/PVE check
→ exclusive Stage 0 lock
→ minimal Git/SSH packages
→ DNS/HTTPS check GitHub
→ временный read-only GitHub Deploy Key
→ проверка read-only доступа к private repo
```

Если Deploy Key уже авторизован, Stage 0 сразу продолжает работу.

Если доступа ещё нет:

```text
вывести public key
→ показать путь GitHub Settings → Deploy keys
→ напомнить Allow write access = OFF
→ ждать Enter через /dev/tty
→ повторно проверить GitHub connectivity
→ один раз повторить read-only Git access check
```

Если после Enter доступ не появился, Stage 0 завершается с явной ошибкой. При следующем запуске существующий временный private key используется повторно, public часть восстанавливается из него и снова показывается человеку.

После успешной авторизации:

```text
проверка origin временного checkout, если он уже существует
→ shallow clone/fetch zsergeyru/proxmox, depth=1
→ запуск private Stage 1
→ после успешного handoff полное удаление /var/lib/proxmox-bootstrap
→ создание постоянного stage0-complete marker
```

Публичный script не содержит внутренние PVE roles, ACL, pools, API identities, VMID plan, template logic, Proximo configuration или deployer implementation.

Временная область Stage 0:

```text
/var/lib/proxmox-bootstrap/
```

существует только до успешного handoff и после него удаляется целиком вместе с временным Deploy Key, `known_hosts`, SSH config и temporary checkout.

Постоянный marker успешного Stage 0 хранится в:

```text
/var/lib/proxmox-deployer/state/stage0-complete
```

---

## Private Stage 1 — реализована

Канонический full bootstrap:

```text
scripts/pve/bootstrap/init-pve.sh
```

Текущая версия private bootstrap:

```text
BOOTSTRAP_VERSION=8
```

Он выполняет:

```text
exclusive run lock
→ state=running
→ host preflight
→ configuration snapshot
→ PVE/Ceph repository policy без subscription
→ packages
→ DNS/time/network checks
→ storage/snippets
→ pvedeploy
→ принятие Deploy Key от Stage 0
→ canonical private checkout + проверка origin
→ managed pool
→ roles/users/API tokens/ACL
→ effective permissions checks
→ real API-token authentication checks
→ capacity/source checks для template
→ protected template 9000
→ local wrappers/status
→ final state
```

### APT и Ceph repository policy

Для основного PVE используется `pve-no-subscription`, а `pve-enterprise` отключается.

Ceph обрабатывается отдельно и консервативно:

```text
ceph.sources отсутствует
→ ничего не менять

ceph.sources уже указывает на download.proxmox.com + no-subscription
→ ничего не менять

ceph.sources содержит enterprise.proxmox.com + Components: enterprise
→ сохранить тот же ceph-* release
→ заменить только канал на download.proxmox.com + no-subscription

активный legacy ceph.list или нестандартный/multi-stanza ceph.sources
→ STOP
→ не переписывать автоматически
```

Bootstrap не выбирает новую Ceph major/release самостоятельно. Например, если существующий repository указывает на `ceph-squid`, при переводе enterprise → no-subscription остаётся именно `ceph-squid`.

Изменение Ceph repository выполняется после configuration snapshot, поэтому исходные APT source files уже сохранены в `/var/backups/proxmox-bootstrap/...`.

### Постоянный bootstrap state

Private Stage 1 больше не использует временный каталог Stage 0 для постоянного состояния.

Bootstrap state хранится в:

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── last-run.json
├── version
├── last-revision
└── stage0-complete
```

`stage0-complete` создаётся public Stage 0 только после успешного возврата из private Stage 1 и удаления временной `/var/lib/proxmox-bootstrap`.

### Поведение проектных ролей

Для проектных ролей применяется additive policy:

```text
роль отсутствует
→ создать

роль существует, но не хватает privileges
→ добавить только недостающие через append

в роли есть дополнительные privileges
→ оставить без изменения
```

Bootstrap не удаляет существующие privileges автоматически.

### Поведение API-токенов

Новый токен создаётся с `privsep=1`.

Для существующих API-токенов bootstrap не меняет `privsep` автоматически. Если обнаружен `privsep=0`, выводится предупреждение и конфигурация токена сохраняется без изменения, чтобы не сломать уже работающий доступ.

После создания/обнаружения токена bootstrap выполняет две дополнительные проверки:

```text
pveum effective permissions
+
реальная локальная авторизация этим token+secret через Proxmox API
```

Если secret-файл существует, но реальный API credential уже не работает, bootstrap останавливается вместо ложного статуса `ready`.

### Существующие PVE users

Если проектный PVE user уже существует, но имеет `enable=0`, bootstrap **не включает его автоматически**.

Такой случай считается конфликтом и останавливает Stage 1 с объяснением. Это нужно, чтобы bootstrap случайно не отменил осознанную блокировку служебной учётной записи.

### Existing ACL

Существующие дополнительные ACL не удаляются автоматически.

Если нужный ACL отсутствует, он добавляется. Если уже существующий ACL отличается по `propagate`, bootstrap выводит предупреждение и не сужает его автоматически.

### Private checkout

Если `${REPO_DIR}` уже содержит Git checkout, перед `fetch/reset` проверяется `origin`.

Ожидается только канонический repository URL проекта. Неожиданный `origin` не переписывается автоматически: bootstrap останавливается и требует явного решения.

### Надёжность повторного запуска

Private Stage 1 использует эксклюзивный `flock`, поэтому два экземпляра bootstrap не могут одновременно изменять PVE.

Состояние запуска записывается в:

```text
/var/lib/proxmox-deployer/state/state.json
/var/lib/proxmox-deployer/state/last-run.json
```

В начале запуска устанавливается `running`. При штатной ошибке и при неожиданной shell-ошибке состояние меняется на `failed`, поэтому старый `ready` не остаётся после неудачного повторного запуска.

### Template safety

Перед первым созданием template выполняется проверка свободного места на `local` и `local-lvm`, а также доступности Debian cloud image.

Для уже существующего VMID `9000` preflight сначала только проверяет:

```text
name = tpl-debian13
template = 1
```

Если VMID `9000` не является именно этим canonical template, bootstrap останавливается и ничего не переписывает.

Preflight **не меняет** `protection`. До любых изменений выполняется configuration snapshot. Если template уже существует, его исходный `qm config 9000` дополнительно записывается в diagnostics snapshot.

Только позже, в `ensure_template`, если canonical template найден без `protection=1`, bootstrap выполняет:

```bash
qm set 9000 --protection 1
```

После этого значение повторно проверяется. Если protection уже включён, изменений не производится.

Таким образом canonical template `9000` приводится к состоянию:

```text
name = tpl-debian13
template = 1
protection = 1
```

но изменение protection всегда происходит уже **после** снимка исходной конфигурации.

Содержимое resource pool `managed` bootstrap специально не анализирует: существующий состав пула считается текущим административным состоянием Proxmox.

---

## Active template builder

Поддерживаемый builder:

```text
scripts/pve/create-template.sh
```

Он создаёт:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 4
```

Модель:

```text
Debian 13 trixie/latest cloud image
→ SHA-512 verification
→ apt update/full-upgrade
→ regular amd64 kernel
→ console/QGA verification
→ cleanup
→ template 9000
→ protection=1
```

Документация:

- [`../templates/debian13/README.md`](../templates/debian13/README.md)
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md)

---

## Access control

Принята схема двух отдельных PVE identities:

```text
deployer@pve!host-deploy
→ человек / deploy-guest / host-side tools

ai-agent@pve!infra
→ AI / Proximo
```

Основная AI write-zone:

```text
managed
```

Новые обычные VM/LXC AI создаёт сразу в `managed`. Protected guests/templates остаются вне этого pool.

Подробности: [`25-pve-access-control.md`](25-pve-access-control.md).

---

## Что ещё не реализовано

Основной оставшийся PVE-side компонент:

```text
scripts/pve/deploy-guest.py
```

После его появления private bootstrap автоматически сможет установить стабильную wrapper-команду:

```text
/usr/local/sbin/deploy-guest
```

До этого private Stage 1 корректно завершает основную настройку со статусом `partial`/предупреждением о недостающем deploy-guest.

Также остаются отдельные последующие задачи:

```text
создание/клонирование VM и LXC по guest.yaml через deploy-guest
bootstrap/configuration 301-ai-control
передача runtime copy AI credential внутрь 301
SSH/Cloud-Init policy для новых guests
health checks для конкретных guest deployments
backup/recovery automation
```

---

## Source of truth

Приватный `zsergeyru/proxmox` хранит:

```text
архитектуру
VMID plan
guest.yaml
network/storage/security policy
private Stage 1 bootstrap
PVE-side deployer
API users/tokens/ACL policy
AI Control и DevOps ADR
template builder/policy
```

Public repo хранит только Stage 0 loader.

---

## Security

Public repo не должен содержать:

- private keys;
- API token secrets;
- passwords;
- реальные `.env`;
- детальную внутреннюю ACL/VMID/runtime configuration.

Deploy Key создаётся на конкретном PVE и выдаётся только на чтение private repository.

До успешного handoff временный private key хранится только в закрытой `/var/lib/proxmox-bootstrap` с режимом `0700` для каталога и `0600` для private key.

Private Stage 1 переносит Deploy Key в каноническое `/etc/proxmox-deployer/ssh`. После успешного handoff временная Stage 0 область удаляется целиком.

При проверке API credential token secret не выводится в лог и не передаётся непосредственно в аргументах `curl`; временный header-файл создаётся с закрытыми правами и удаляется сразу после проверки.

---

## Главный принцип

> Public Stage 0 только открывает безопасный read-only путь к private source of truth и после успешной передачи управления исчезает как временный runtime. Всё, что постоянно конфигурирует Proxmox и описывает внутреннее устройство инфраструктуры, находится и развивается только в `zsergeyru/proxmox`.
