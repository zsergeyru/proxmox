# AI Control bootstrap

## Статус

**Требует полного перепроектирования.**

Предыдущая реализация bootstrap `301-ai-control` больше не считается канонической или поддерживаемой.

Ранее использовавшиеся public scripts:

```text
create-ai-control-vm.sh
prepare-ai-control.sh
install-ai-agent.sh
bootstrap-ai-control.sh
```

перенесены в:

```text
zsergeyru/proxmox-bootstrap/archive/2026-09-14/
```

Они сохранены только для истории и анализа предыдущих решений. Для нового развёртывания их использовать не следует.

## Что будет определено заново

Перед новой реализацией нужно отдельно принять решения по:

- способу создания `301-ai-control`;
- границе между generic guest deploy и специальным AI bootstrap;
- доступу AI к PVE;
- SSH/Git identities;
- хранению и восстановлению credentials;
- выбору MCP/API слоя;
- установке Docker или другого runtime;
- установке и замене AI agent;
- порядку bootstrap до появления самого AI control plane;
- update/recovery policy;
- idempotency и rollback.

Ни один из этих пунктов не должен автоматически наследовать старую реализацию только потому, что она существует в archive.

## Что пока сохраняется

VMID `301` остаётся зарезервирован в общей инфраструктурной модели как `ai-control`, пока отдельно не принято решение его изменить.

Базовый template `9000 tpl-debian13` остаётся действующим и создаётся активным `create-template.sh`.

Остальная AI bootstrap цепочка — **TBD**.

См. также:

- [`31-bootstrap.md`](31-bootstrap.md) — текущий статус bootstrap;
- [`50-ai-control.md`](50-ai-control.md) — статус архитектуры AI Control.
