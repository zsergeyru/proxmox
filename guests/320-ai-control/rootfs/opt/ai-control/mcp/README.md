# MCP

Каталог содержит конфигурации и исходники MCP-сервисов, используемых агентами `320-ai-control`.

MCP являются общими capabilities управляющего контура и не дублируются внутри `agents/<name>/`. Hermes, Agent Zero и будущие агенты могут использовать один и тот же MCP backend при наличии соответствующего доступа.

## Proxmox

На `320-ai-control` уже установлен и принят как общий Proxmox MCP:

```text
gordcurrie/proxmox-mcp
```

Архитектурное решение и safety model описаны в [`../../../../decisions/003-proxmox-mcp.md`](../../../../decisions/003-proxmox-mcp.md).

Он выбран в том числе из-за HTTP transport: MCP работает как отдельный общий сервис для нескольких AI-агентов, а не как stdio-only процесс одного клиента.

Базовое разделение ответственности:

```text
Proxmox MCP
→ управление уровнем виртуализации: VM/LXC, ресурсы, lifecycle, snapshots, backups, status/tasks

Ansible на 311-dev-services
→ повторяемая конфигурация гостевых ОС и приложений

SSH
→ bootstrap, диагностика, аварийные и разовые действия внутри гостевых ОС

другие MCP
→ специализированные API там, где они реально удобнее обычного SSH/API
```

`deploy-mcp` не является частью целевой архитектуры.

### Безопасность

По умолчанию destructive tools должны оставаться выключенными:

```text
PROXMOX_ALLOW_DESTRUCTIVE=false
```

Host-level write operations, особенно изменение сети PVE node, storage configuration, ACL/IAM, SDN и reboot/shutdown гипервизора, не входят в штатную зону AI control.

Read-only host/network диагностика допустима. Возможность MCP предоставить какой-либо tool не означает, что API token должен иметь право его выполнить.

Права общего Proxmox MCP определяются отдельным Proxmox API token и ACL согласно [`../../../../decisions/002-proxmox-permissions.md`](../../../../decisions/002-proxmox-permissions.md).

Секреты, токены, пароли и приватные ключи в Git не добавлять.
