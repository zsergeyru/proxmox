# 320 — observed status

Фактическое состояние снято с Proxmox 2026-09-12 и дополнено фактом установленного Proxmox MCP.

```text
VMID: 320
Proxmox name: ai-agent
status: running
vCPU: 2
RAM: 4096 MiB
system disk: 16 GiB
QEMU Guest Agent: enabled
network bridge: vmbr0
Proxmox firewall on net0: enabled
CPU type: x86-64-v2-AES
```

Системный диск:

```text
local-lvm:vm-320-disk-0
size: 16 GiB
controller: virtio-scsi-single
```

К VM всё ещё подключён установочный ISO:

```text
local:iso/debian-13.6.0-amd64-netinst.iso
```

В фактическом выводе `qm config 320` параметр `onboot` отсутствует, то есть текущая VM не подтверждена как настроенная на автоматический запуск.

Это отличается от желаемого `guest.yaml`, где `boot.onboot: true`. Пока настройка на сервере не изменена, это считается известным drift между desired state и observed state.

## AI control / Proxmox MCP

На VM `320-ai-control` установлен выбранный общий Proxmox MCP:

```text
gordcurrie/proxmox-mcp
```

Он используется как общий MCP backend управляющего контура и рассчитан на доступ нескольких AI-агентов через HTTP transport.

Архитектура и ограничения описаны в [`decisions/003-proxmox-mcp.md`](decisions/003-proxmox-mcp.md). Права Proxmox API token/ACL описаны в [`decisions/002-proxmox-permissions.md`](decisions/002-proxmox-permissions.md).

Важно: наличие в MCP host/network tools не означает, что они разрешены проектом. Host-level write operations должны блокироваться ограниченным Proxmox token/ACL; destructive tools по умолчанию отключены.

Архитектурное имя каталога остаётся `320-ai-control`, а текущее имя объекта в Proxmox — `ai-agent`. Переименование работающей VM не требуется только ради документации; вопрос можно решить при миграции на целевой `301-ai-control`.
