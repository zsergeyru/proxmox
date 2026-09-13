# ADR 003 — Общий Proxmox MCP для AI control

## Статус

Принято для bootstrap `320-ai-control`; переносится без изменения принципа на будущий `301-ai-control`.

## Фактическое состояние

На VM `320-ai-control` уже установлен и используется:

```text
gordcurrie/proxmox-mcp
```

Это текущий выбранный общий Proxmox MCP проекта.

## Контекст

`ai-control` является мультиагентным контуром. В `/opt/ai-control/agents/` могут одновременно существовать Hermes, Agent Zero и другие агенты.

Поэтому Proxmox integration не должна быть зашита внутрь каталога одного агента. Общий MCP-сервис размещается в:

```text
/opt/ai-control/mcp/
```

и может использоваться несколькими агентами.

## Почему выбран `gordcurrie/proxmox-mcp`

При выборе рассматривались другие Proxmox MCP, но один из первоначально понравившихся вариантов не имел подходящего HTTP transport. Для нашей архитектуры это было существенным ограничением: MCP должен работать как отдельный общий сервис и быть доступен нескольким агентам, а не запускаться только как локальный stdio-процесс одного клиента.

`gordcurrie/proxmox-mcp` был выбран потому, что сочетает:

- HTTP transport для отдельного общего MCP-сервиса;
- работу через Proxmox API;
- VM и LXC inventory/config/status;
- create/clone/configure;
- start/stop/reboot VM и LXC;
- snapshots и backups;
- асинхронные Proxmox tasks;
- поддержку API token и стандартной Proxmox ACL-модели;
- дополнительное ограничение destructive tools.

Для нашей мультиагентной схемы HTTP transport принципиально удобнее stdio-only варианта:

```text
Hermes ---------┐
Agent Zero -----+--> shared gordcurrie/proxmox-mcp --> Proxmox API
Future agent ---┘
```

## Destructive operations

У MCP есть отдельный предохранитель для destructive-команд. Они не должны быть доступны в обычном режиме и включаются только отдельной настройкой сервиса.

Текущая политика проекта:

```text
PROXMOX_ALLOW_DESTRUCTIVE=false
```

по умолчанию.

Delete VM/LXC, node reboot/shutdown и другие destructive tools включаются только отдельным осознанным решением. Даже при включении destructive tool окончательное разрешение всё равно задаётся Proxmox API token и ACL.

## Host/network tools

Важный недостаток выбранного MCP — он умеет не только управлять гостями, но и выполнять host-level операции, в том числе работать с сетевой конфигурацией PVE node.

Для проекта это считается слишком опасным для обычного AI-agent workflow.

Агентам полезно оставить read-only диагностику сети PVE, например просмотр интерфейсов и текущей конфигурации, но write-операции уровня host network не должны быть доступны для выполнения.

Запрещённая зона включает изменение:

```text
PVE physical NIC
Linux bridge / vmbr*
host routes
gateway
/etc/network/interfaces
apply/reload host network configuration
```

Даже если соответствующие tools присутствуют в MCP, API token не должен иметь Proxmox privileges/ACL, позволяющие выполнить эти операции.

Иными словами, безопасность строится в два слоя:

```text
MCP tool policy
        ↓
Proxmox API token + ACL
        ↓
managed guest/resource pool
```

Скрытие или отключение опасного MCP tool является дополнительной защитой, но не заменяет ACL.

## Storage и другие host-level возможности

Аналогичный принцип применяется к storage и прочим возможностям самого гипервизора.

Разрешается использовать уже существующий storage для операций с управляемыми гостями: clone, disk allocation и backup в рамках выданных Proxmox прав.

Не разрешается через обычный AI control workflow:

- создавать/удалять/перенастраивать PVE storage;
- менять Datacenter/host firewall;
- менять ACL/users/roles/API tokens;
- управлять SDN;
- reboot/shutdown самого PVE host;
- менять сертификаты, repositories или конфигурацию гипервизора.

Полная матрица прав описана в [`002-proxmox-permissions.md`](./002-proxmox-permissions.md).

## Разделение функций

Proxmox MCP используется для уровня виртуализации:

```text
VM/LXC lifecycle
resource/config changes
snapshots
backups
status/tasks
```

Повторяемая настройка ОС и приложений остаётся за Ansible на `311-dev-services`.

Прямой SSH остаётся bootstrap/diagnostic/emergency каналом.

Если MCP умеет выполнять дополнительные команды внутри гостей или на PVE host, это не делает их штатным deploy-механизмом проекта.

## Management identity

MCP подключается к PVE через отдельный API user/token с privilege separation.

Права определяются ADR [`002-proxmox-permissions.md`](./002-proxmox-permissions.md), а не полным набором capabilities самого MCP.

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

## Обновление

MCP не обновляется автоматически на непроверенную версию.

Порядок обновления:

1. зафиксировать текущую рабочую версию;
2. проверить release notes и изменение tool surface;
3. проверить, не появились ли новые host/network/destructive tools;
4. убедиться, что Proxmox ACL по-прежнему ограничивает их;
5. проверить read operations;
6. проверить lifecycle на тестовом managed guest;
7. только после этого обновлять production AI control.

## Главный принцип

> `gordcurrie/proxmox-mcp` — общий transport/API слой AI control; конкретный AI-агент является сменяемым клиентом, а окончательная граница полномочий задаётся Proxmox API token и ACL.
