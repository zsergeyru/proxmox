# Bootstrap — текущее состояние

## Статус

Bootstrap PVE теперь разделён на две исполняемые стадии.

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

Публичный `zsergeyru/proxmox-bootstrap` теперь содержит только zero-day loader.

Он выполняет:

```text
root/PVE check
→ minimal Git/SSH packages
→ временный read-only GitHub Deploy Key
→ ожидание добавления key в private repo
→ проверка read-only доступа
→ временный clone zsergeyru/proxmox
→ запуск private Stage 1
→ cleanup временного key/checkout после успешного handoff
```

Публичный script не содержит внутренние PVE roles, ACL, pools, API identities, VMID plan, template logic, Proximo configuration или deployer implementation.

После успешного handoff остаётся marker:

```text
/var/lib/proxmox-bootstrap/stage0-complete
```

---

## Private Stage 1 — реализована

Канонический full bootstrap:

```text
scripts/pve/bootstrap/init-pve.sh
```

Он выполняет:

```text
host preflight
configuration snapshot
pve-no-subscription
packages
DNS/time/network checks
storage/snippets
pvedeploy
принятие Deploy Key от Stage 0
canonical private checkout
managed pool
roles/users/API tokens/ACL
protected template 9000
local wrappers/status
```

Существующие роли, которые отличаются от ожидаемой модели, не переписываются автоматически: выводится warning с missing/extra privileges, после чего bootstrap продолжается.

Существующие дополнительные ACL также не удаляются автоматически.

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

Public repo хранит только Stage 0 loader и архив старых публичных экспериментов.

---

## Security

Public repo не должен содержать:

- private keys;
- API token secrets;
- passwords;
- реальные `.env`;
- детальную внутреннюю ACL/VMID/runtime configuration.

Deploy Key создаётся на конкретном PVE и выдаётся только на чтение private repository.

После успешного handoff временная Stage 0 копия credential удаляется, а каноническая copy хранится по private filesystem policy.

---

## Главный принцип

> Public Stage 0 только открывает безопасный read-only путь к private source of truth. Всё, что реально конфигурирует Proxmox и описывает внутреннее устройство инфраструктуры, находится и развивается только в `zsergeyru/proxmox`.
