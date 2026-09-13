# ADR 003 — Общий Proxmox MCP для AI control

## Статус

Принято для bootstrap `320-ai-control`; переносится без изменения принципа на будущий `301-ai-control`.

## Контекст

`ai-control` является мультиагентным контуром. В `/opt/ai-control/agents/` могут одновременно существовать Hermes, Agent Zero и другие агенты.

Поэтому Proxmox integration не должна быть зашита внутрь каталога одного агента. Общий MCP-сервис размещается в:

```text
/opt/ai-control/mcp/
```

и может использоваться несколькими агентами.

Установленный ранее `proxmox-full` относится к agent skill/tooling и не считается общим архитектурным Proxmox MCP backend.

## Выбранный MCP

Основной кандидат и принятый backend:

```text
lidless-labs/proxmox-mcp
npm package: @solomonneas/proxmox-mcp
```

При развёртывании фиксируется конкретная проверенная версия, а не плавающий `latest`.

## Почему выбран он

Требования проекта:

- Proxmox API-token authentication;
- работа без root shell на PVE host для штатных guest lifecycle операций;
- VM и LXC inventory/config/status;
- create/clone/configure/start/stop/reboot;
- snapshots и backups;
- restore в безопасном сценарии;
- работа с scoped Proxmox ACL/resource pool;
- отдельный safety layer поверх самих Proxmox ACL;
- возможность использовать один общий MCP несколькими AI agents.

`lidless-labs/proxmox-mcp` соответствует этим требованиям и дополнительно использует write gates:

```text
read
→ без confirm

safe write
→ confirm=true

destructive
→ confirm=true
→ destructive=true
→ PROXMOX_ENABLE_DESTRUCTIVE=1 на процессе MCP
```

Таким образом действуют два независимых защитных слоя:

```text
MCP write/destructive gates
        ↓
Proxmox API token + ACL
        ↓
managed guest/resource pool
```

Даже если MCP зарегистрировал инструмент для host-level или IAM операции, Proxmox token не должен иметь соответствующего privilege/path ACL.

## Разделение функций

Proxmox MCP используется только для уровня виртуализации:

```text
VM/LXC lifecycle
resource/config changes
snapshots
backups
status/tasks
```

Повторяемая настройка ОС и приложений остаётся за Ansible на `311-dev-services`.

Прямой SSH остаётся отдельным bootstrap/diagnostic/emergency каналом.

Встроенные возможности MCP для выполнения команд внутри гостей не являются штатным deploy-механизмом проекта и не заменяют Ansible/SSH модель.

## Management identity

MCP подключается к PVE через отдельный API user/token с privilege separation.

Права определяются ADR [`002-proxmox-permissions.md`](./002-proxmox-permissions.md), а не возможностями самого MCP.

Запрещено использовать для общего MCP:

- `root@pam` token;
- `PVEAdmin` на `/`;
- token, способный менять ACL/roles/users/tokens;
- token с правом reboot/shutdown самого PVE host;
- token с правом менять host network/storage configuration/SDN без отдельного решения.

## Мультиагентная модель

Каталоги агентов остаются независимыми:

```text
/opt/ai-control/agents/hermes/
/opt/ai-control/agents/agent-zero/
/opt/ai-control/agents/<future-agent>/
```

Общий MCP не копируется в каждый каталог:

```text
/opt/ai-control/mcp/proxmox/
```

Агент получает доступ к MCP как к общей capability управляющего контура.

При необходимости можно ограничивать, какой агент вообще подключён к MCP, но это отдельный application-level policy и не заменяет Proxmox ACL.

## `proxmox-full`

`proxmox-full`, установленный ранее в bootstrap-контуре, рассматривается как agent skill: он помогает агенту правильно применять Proxmox capabilities, но не является security boundary и не заменяет общий MCP/API token.

То есть:

```text
agent skill `proxmox-full`
→ инструкции/agent capability

shared Proxmox MCP
→ структурированный интерфейс к Proxmox API

Proxmox token + ACL
→ фактическая авторизация
```

## Обновление

MCP не обновляется автоматически на непроверенный major/minor release.

Порядок обновления:

1. зафиксировать текущую рабочую версию;
2. проверить release notes/tool surface;
3. убедиться, что новые tools не требуют расширения PVE privileges без отдельного решения;
4. проверить read operations;
5. проверить lifecycle на тестовом managed guest;
6. только после этого обновлять production AI control.

## Главный принцип

> MCP — общий инструмент AI control, агент — сменяемый клиент, а окончательная граница полномочий задаётся Proxmox API token и ACL.
