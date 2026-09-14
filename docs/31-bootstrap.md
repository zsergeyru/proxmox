# Bootstrap — текущее состояние

## Статус

Архитектура bootstrap/deploy **не пересматривается**. С нуля переписывается только её исполняемая реализация.

Канонические архитектурные решения остаются в:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- guest manifests и ADR соответствующих VM/LXC.

В публичном `zsergeyru/proxmox-bootstrap` сейчас активным и поддерживаемым остаётся только:

```text
create-template.sh
```

Он создаёт базовый Debian 13 template `9000 tpl-debian13`.

Прежние executable-сценарии общего bootstrap и AI Control перенесены в:

```text
zsergeyru/proxmox-bootstrap/archive/2026-09-14/
```

Архивные скрипты не являются рабочими entrypoints и не должны использоваться для нового развёртывания. Новые скрипты будут написаны заново по действующей документации.

## Что нужно реализовать заново

Нужно заново написать и проверить код, реализующий уже принятую архитектуру, в том числе:

```text
public zero-day init PVE
private PVE-side deployer
создание/клонирование VM и LXC по guest.yaml
bootstrap/configuration 301-ai-control
установку runtime и AI agent
передачу SSH/Git credentials по принятой модели
health checks, idempotency и recovery logic
```

Названия и внутренняя структура новых скриптов могут измениться. Архивные реализации не являются шаблоном, который нужно механически восстанавливать.

## Что остаётся source of truth

Приватный `zsergeyru/proxmox` продолжает хранить:

```text
архитектуру
VMID plan
guest.yaml
schema/validator
network/storage/security policy
PVE bootstrap/deployer requirements
AI Control и DevOps ADR
решения по template
```

То есть различаем:

```text
архитектура / desired state → действуют
старые scripts             → сняты и переписываются
```

## Активный template builder

Текущий поддерживаемый public script:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Он создаёт:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 4
```

Версия 4 намеренно использует простую модель:

```text
trixie/latest Debian cloud image
→ SHA-512 verification
→ обычные Debian repositories
→ apt update/full-upgrade
→ regular amd64 kernel
→ console/QGA verification
→ cleanup
→ template 9000
```

Pinned Debian build, APT snapshot и отдельный version lock для template не используются.

Документация template:

- [`../templates/debian13/README.md`](../templates/debian13/README.md)
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md)

## Правило для новой реализации

При написании новых скриптов сначала читается действующая архитектура, затем код реализует её. Если в процессе обнаруживается, что архитектурное решение нужно изменить, это оформляется отдельно как изменение документации/ADR, а не молча меняется внутри shell/python-кода.

Особенно это относится к:

- public/private границе bootstrap;
- PVE Git checkout;
- API users/tokens/ACL;
- managed pool и protected objects;
- 301-ai-control;
- SSH identities;
- Ansible/Semaphore на 311;
- backup/recovery и secrets.

## Security

Public repo не должен содержать secrets, private keys, API token secrets, passwords или реальные `.env`.

Новая реализация обязана соблюдать уже принятые security boundaries и filesystem/credential policy проекта.
