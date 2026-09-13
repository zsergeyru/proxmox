# Roadmap

## Ближайшие задачи

1. Проверить automount USB storage `/mnt/pve/backup`; контрольной проверкой подтвердить отсутствие `nic1` и убрать остаточную строку `iface nic1 inet manual` из рабочего конфига PVE.
2. Завершить базовый Debian 13 template без `virt-customize`, проверить `ops`, QEMU Guest Agent, serial console и Cloud-Init.
3. Развернуть `109-network-gateway` в текущей LAN по адресу `192.168.1.9`: SmartDNS, исходящие VPN, PBR, remote-access VPN и health-check, сохранив обычный интернет через Keenetic независимо от 109.
4. Проверить будущую MAIN `10.0.0.0/16` ещё до переезда. Для planned-гостей уже зафиксированы пары `current`/`target`; миграцию выполнять поэтапно с одним default gateway.
5. Развернуть `311-dev-services`: Ansible как штатный повторяемый deploy по SSH; Semaphore как необязательный ручной web-интерфейс; Git/CI добавлять только по необходимости.
6. Реализовать минимальные Ansible playbook для применения `rootfs/`: `/opt/<service>/` можно синхронизировать как принадлежащий проекту каталог, системные файлы — только по конкретным путям.
7. Подготовить `301-ai-control`, проверить Hermes, Proxmox MCP, SSH и вызов Ansible на 311; только после этого вывести bootstrap `320`.
8. Определить startup/shutdown order гостей. Аварийный доступ к PVE не должен зависеть от 109, 301 или 311.
9. Настроить основное SMB/CIFS backup-хранилище, проверить USB-копию, ввести retention и выполнить первый документированный restore-test.
10. Перед переездом внедрить OpenWrt как edge-router: WAN, VLAN termination, DHCP и базовый L3/firewall оставить на физическом роутере; DNS/VPN/PBR оставить на 109.
11. Проверить IPv6 провайдера новой квартиры и только после этого включать dual stack.
12. Выбрать VM/LXC для `501-frigate` после практического теста iGPU/OpenVINO.
13. Переносить из `appart-rennovation` только серверные эксплуатационные сведения без дублирования общего проекта квартиры.

## Уже зафиксировано

- PVE: Intel N150, 16 GiB RAM, SSD 512 GiB, одна физическая Ethernet-карта;
- текущий PVE: `192.168.1.11/16`, gateway `192.168.1.1`, `vmbr0 → nic0`;
- VM `100 HAOS` — действующая production-система и не мигрирует только ради новой VMID-схемы;
- VM `320-ai-control` — временный bootstrap, целевой control plane — `301`;
- Debian-инфраструктура использует единого SSH-пользователя `ops`;
- до переезда VLAN не используются;
- после переезда OpenWrt отвечает за WAN/VLAN/DHCP/L3/firewall, а 109 — за DNS/VPN/PBR/remote-access;
- planned-серверы используют единое правило `VMID XYZ → 192.168.X.YZ` сейчас и `VMID XYZ → 10.0.X.YZ` в целевой сети;
- `109-network-gateway`: `192.168.1.9/16` сейчас, `10.0.1.9/16` в целевой сети;
- MAIN `10.0.0.0/16`, IOT `10.20.0.0/16`, CAMERAS `10.30.0.0/16`, REMOTE-VPN `10.60.0.0/16`;
- `home.arpa` — внутренний DNS-домен, `.local` — mDNS;
- SmartDNS — основной DNS-движок; AdGuard Home опционален; sing-box используется точечно;
- Ansible на 311 — штатный повторяемый deploy, Semaphore — необязательный UI;
- `rootfs/` хранит только управляемые проектом файлы и не включает persistent data;
- backup: SMB/CIFS как основная внешняя копия + USB как дополнительная; для критичных систем базово 7 daily + 4 weekly и restore-test не реже раза в три месяца;
- запланированный VMID не означает обязательное немедленное создание гостя.

Статусы конкретных гостей ведутся в `guest.yaml` и при необходимости `STATUS.md`. VMID/CTID определяются только [`vmid-plan.md`](vmid-plan.md).
