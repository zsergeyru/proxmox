# Roadmap

## Ближайшие задачи

1. Проверить automount USB storage `/mnt/pve/backup`; подтвердить отсутствие `nic1` и убрать остаточную строку `iface nic1 inet manual` из рабочего конфига PVE.
2. Завершить базовый Debian 13 template без `virt-customize`, проверить `ops`, QEMU Guest Agent, serial console и Cloud-Init.
3. Реализовать `scripts/pve/deploy-guest.py`: PLAN по умолчанию, `--apply`, safe reconciliation, task wait и verify.
4. Перед сетевой миграцией реализовать последовательное переключение central `subnet + gateway`: новый IP каждого обычного гостя вычислять из VMID, применять по одному гостю и проверять доступность после переключения.
5. Развернуть `109-network-gateway` в текущей LAN: SmartDNS, исходящие VPN, PBR, remote-access VPN и health-check, сохранив обычный интернет через Keenetic независимо от 109.
6. Развернуть `311-dev-services`: Ansible как штатный повторяемый deploy по SSH; Semaphore как необязательный ручной web-интерфейс.
7. Реализовать минимальные Ansible playbook для применения `rootfs/`.
8. Подготовить `301-ai-control`, проверить Hermes, Proxmox MCP, SSH и вызов Ansible на 311; только после этого вывести bootstrap `320`.
9. Определить startup/shutdown order гостей. Аварийный доступ к PVE не должен зависеть от 109, 301 или 311.
10. Настроить SMB/CIFS backup-хранилище, проверить USB-копию, retention и первый документированный restore-test.
11. Перед переездом внедрить OpenWrt как edge-router: WAN, VLAN termination, DHCP и базовый L3/firewall оставить на физическом роутере; DNS/VPN/PBR оставить на 109.
12. Проверить IPv6 провайдера новой квартиры и только после этого включать dual stack.
13. Выбрать VM/LXC для `501-frigate` после практического теста iGPU/OpenVINO.
14. Переносить из `appart-rennovation` только серверные эксплуатационные сведения без дублирования общего проекта квартиры.

## Уже зафиксировано

- PVE: Intel N150, 16 GiB RAM, SSD 512 GiB, одна физическая Ethernet-карта;
- текущий PVE: `192.168.1.11/16`, gateway `192.168.1.1`, `vmbr0 → nic0`;
- VM `100 HAOS` — действующая production-система;
- VM `320-ai-control` — временный bootstrap, целевой control plane — `301`;
- Debian-инфраструктура использует SSH-пользователя `ops`;
- до переезда VLAN не используются;
- после переезда OpenWrt отвечает за WAN/VLAN/DHCP/L3/firewall, а 109 — за DNS/VPN/PBR/remote-access;
- guest network имеет один active subnet, один gateway и один management IP на гостя;
- active subnet/gateway хранятся только в `guests/defaults.yaml`;
- обычный management IP вычисляется по VMID; отдельный IP хранится только как optional override;
- текущая central сеть — `192.168.0.0/16`, gateway `192.168.1.1`;
- будущая MAIN — `10.0.0.0/16`, gateway `10.0.0.1`;
- IOT `10.20.0.0/16`, CAMERAS `10.30.0.0/16`, REMOTE-VPN `10.60.0.0/16`;
- `home.arpa` — внутренний DNS-домен, `.local` — mDNS;
- SmartDNS — основной DNS-движок; AdGuard Home опционален; sing-box используется точечно;
- Ansible на 311 — штатный повторяемый deploy, Semaphore — необязательный UI;
- `rootfs/` хранит только управляемые проектом файлы и не включает persistent data;
- backup: SMB/CIFS как основная внешняя копия + USB как дополнительная; для критичных систем базово 7 daily + 4 weekly и restore-test не реже раза в три месяца;
- запланированный VMID не означает обязательное немедленное создание гостя.

Статусы конкретных гостей ведутся в `guest.yaml` и при необходимости `STATUS.md`.
