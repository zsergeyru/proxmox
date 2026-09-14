# Bootstrap — текущее состояние

## Статус

По состоянию на 2026-09-14 принято решение **перезапустить проектирование bootstrap/deploy с нуля**.

В публичном `zsergeyru/proxmox-bootstrap` активным и поддерживаемым остаётся только:

```text
create-template.sh
```

Он создаёт базовый Debian 13 template `9000 tpl-debian13`.

Все прежние сценарии AI Control и общего bootstrap перенесены в:

```text
zsergeyru/proxmox-bootstrap/archive/2026-09-14/
```

Архивные файлы не являются рабочими entrypoints и не должны использоваться для нового развёртывания.

## Что пока не считается реализованным

Следующие части будут спроектированы заново и до нового решения считаются **TBD**:

```text
init-pve.sh
PVE zero-day bootstrap
deploy-guest
универсальное создание VM/LXC
bootstrap 301-ai-control
установка AI agent/runtime
PVE ↔ AI permission model в части bootstrap workflow
normal/recovery orchestration
```

Предыдущие реализации сохранены только в Git history и public archive как материал для анализа.

## Что остаётся source of truth

Приватный `zsergeyru/proxmox` продолжает хранить:

```text
архитектуру
VMID plan
guest.yaml
schema/validator
network/storage/security policy
решения по template
```

Но наличие planned/deployable manifest само по себе **не означает**, что универсальный deployer уже реализован.

## Активный template builder

Текущий поддерживаемый public script:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Текущая реализация создаёт:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 5
```

Документация template:

- [`../templates/debian13/README.md`](../templates/debian13/README.md)
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md)

Именно template builder остаётся единственным executable bootstrap-компонентом, который сейчас считаем действующим.

## Будущая zero-day архитектура

Старая схема больше не считается канонической. Новую цепочку определим отдельно перед реализацией.

До этого нельзя ссылаться на прежние `create-ai-control-vm.sh`, `prepare-ai-control.sh`, `install-ai-agent.sh` или `bootstrap-ai-control.sh` как на рабочий путь.

## Security

Public repo по-прежнему не должен содержать secrets, private keys, API token secrets, passwords или реальные `.env`.

Новая модель credentials, Git access, PVE roles/tokens и recovery будет зафиксирована только после нового проектирования bootstrap.
