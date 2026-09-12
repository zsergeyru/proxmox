# Proxmox host: `pve`

Этот каталог предназначен только для конфигурации самого гипервизора.

Здесь должны храниться воспроизводимые настройки:

- `network/` — bridge/VLAN и host networking;
- `storage/` — описание storage и mount/config decisions;
- `backup/` — host-level backup policy и связанные скрипты.

Прикладные сервисы на Proxmox host не устанавливать. Home Assistant, DNS/VPN, AI, monitoring, Frigate и обычные Docker-приложения должны жить в VM/LXC.
