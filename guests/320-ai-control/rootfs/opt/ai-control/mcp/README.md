# MCP on 320-ai-control

Каталог отражает MCP-сервисы текущего bootstrap-узла `320-ai-control`.

## Фактическое состояние 320

На `320-ai-control` установлен канонический MCP проекта:

```text
Proximo / proximo-proxmox
```

`320` уже мигрирован на Proximo. Его MCP backend больше не отличается от целевого выбора для нового `301-ai-control`.

## Целевой 301

Для `301-ai-control` используется тот же канонический MCP:

```text
Proximo / proximo-proxmox
```

Подробное решение: [`../../../../decisions/003-proxmox-mcp.md`](../../../../decisions/003-proxmox-mcp.md) и [`../../../../../301-ai-control/README.md`](../../../../../301-ai-control/README.md).

Целевая схема:

```text
Hermes
→ local stdio Proximo
→ Proxmox API token + ACL
→ managed guests
```

Proximo ограничивается как минимум двумя уровнями:

```text
PROXIMO_TOOLSETS=pve.guests
        ↓
privilege-separated Proxmox token/ACL
```

Host-level write operations — PVE network, storage definitions, ACL/IAM, SDN, repositories, certificates и reboot/shutdown самого гипервизора — не входят в штатную зону AI control.

## Разделение ответственности

```text
Proximo
→ уровень виртуализации и guest-level Proxmox operations

Ansible на 311-dev-services
→ повторяемая конфигурация гостевых ОС

SSH из ai-control
→ bootstrap, диагностика, аварийные и разовые действия
```

`deploy-mcp` не является частью целевой архитектуры.

Секреты, API token secrets и private SSH keys в Git не добавляются.
