# MCP

Каталог содержит конфигурации и исходники MCP-сервисов, используемых агентами `320-ai-control`.

MCP являются общими capabilities управляющего контура и не дублируются внутри `agents/<name>/`. Hermes, Agent Zero и будущие агенты могут использовать один и тот же MCP backend при наличии соответствующего доступа.

## Proxmox

Для общего Proxmox MCP принят:

```text
lidless-labs/proxmox-mcp
@solomonneas/proxmox-mcp
```

Архитектурное решение и safety model описаны в [`../../../decisions/003-proxmox-mcp.md`](../../../decisions/003-proxmox-mcp.md).

`proxmox-full`, установленный ранее как agent skill, не заменяет общий MCP backend и не является границей авторизации.

Базовое разделение ответственности:

```text
Proxmox MCP
→ управление уровнем виртуализации: VM/LXC, ресурсы, lifecycle, snapshots, backups

Ansible на 311-dev-services
→ повторяемая конфигурация гостевых ОС и приложений

SSH
→ bootstrap, диагностика, аварийные и разовые действия внутри гостевых ОС

другие MCP
→ специализированные API там, где они реально удобнее обычного SSH/API
```

`deploy-mcp` не является частью целевой архитектуры.

Права общего Proxmox MCP определяются отдельным Proxmox API token и ACL согласно [`../../../decisions/002-proxmox-permissions.md`](../../../decisions/002-proxmox-permissions.md). Возможность MCP зарегистрировать некоторый tool не означает, что token должен иметь право выполнить соответствующую host-level операцию.

Секреты, токены, пароли и приватные ключи в Git не добавлять.
