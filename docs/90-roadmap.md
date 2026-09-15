# Roadmap

## Ближайшие задачи

1. Проверить automount USB storage `/mnt/pve/backup`; подтвердить отсутствие `nic1` и убрать остаточную строку `iface nic1 inet manual` из рабочего конфига PVE.
2. Осознанно заменить существующий Debian VM template `9000` на Template-Version 6 и выполнить clean smoke-test: root key-only SSH, QEMU Guest Agent, noVNC/serial console, Cloud-Init, unique host keys и disk growth.
3. Реализовать `scripts/pve/deploy-guest.py`: PLAN по умолчанию, `--apply`, safe reconciliation, task wait и verify; использовать общий `scripts/guest_config.py`.
4. В `deploy-guest` реализовать initial management readiness независимо от bootstrap capabilities: VM получает root keys через Cloud-Init, LXC — через `ssh-public-keys`, после start проверяется `root:22`.
5. Реализовать ограниченный whitelist `bootstrap.capabilities` (`base`, `git`, `docker`, `ansible_controller`) поверх уже работающего root SSH, а не как механизм получения доступа.
6. Перед сетевой миграцией реализовать последовательное переключение central `subnet + gateway`: новый IP каждого обычного гостя вычислять из VMID, применять по одному гостю и проверять доступность.
7. Развернуть `109-network-gateway` в текущей LAN: SmartDNS, исходящие VPN, PBR, remote-access VPN и health-check, сохранив обычный интернет через Keenetic независимо от 109.
8. Развернуть `311-dev-services`: Git + Docker + контейнеризированный Ansible Execution Environment; отдельная Ansible SSH identity авторизуется как root на target guests.
9. Реализовать универсальный Ansible provisioning playbook; Semaphore, Git service, CI и прочие приложения 311 устанавливать уже provisioning-слоем.
10. Подготовить `301-ai-control`: common Proximo, отдельный `ai_control_ed25519`, Git identity и AI agent; проверить прямой root SSH AI в тестовый guest и независимость SSH key от pool `managed`.
11. После проверки 301 вывести bootstrap `320` отдельным решением.
12. Определить startup/shutdown order гостей. Аварийный доступ к PVE не должен зависеть от 109, 301 или 311.
13. Настроить SMB/CIFS backup-хранилище, проверить USB-копию, retention и первый документированный restore-test.
14. Перед переездом внедрить OpenWrt как edge-router: WAN, VLAN termination, DHCP и базовый L3/firewall оставить на физическом роутере; DNS/VPN/PBR оставить на 109.
15. Проверить IPv6 провайдера новой квартиры и только после этого включать dual stack.
16. Выбрать VM/LXC для `501-frigate` после практического теста iGPU/OpenVINO.
17. Переносить из `appart-rennovation` только серверные эксплуатационные сведения без дублирования общего проекта квартиры.

## Уже зафиксировано

- PVE: Intel N150, 16 GiB RAM, SSD 512 GiB, одна физическая Ethernet-карта;
- текущий PVE: `192.168.1.11/16`, gateway `192.168.1.1`, `vmbr0 → nic0`;
- VM `100 HAOS` — действующая production-система;
- VM `320-ai-control` — временный bootstrap, целевой control plane — `301`;
- Private Stage 1 — `BOOTSTRAP_VERSION=12`;
- текущий project contract VM template — `Template-Version=6`;
- управляемая Debian-инфраструктура использует единого management user `root`;
- root password locked, SSH password authentication disabled, доступ только по public keys;
- host-side deployer, AI Control и Ansible используют разные SSH identities одного root account;
- PVE pool `managed` и direct guest SSH являются независимыми access mechanisms;
- AI direct SSH к конкретному guest отзывается удалением `ai_control_ed25519.pub`, а не перемещением guest из pool;
- до переезда VLAN не используются;
- после переезда OpenWrt отвечает за WAN/VLAN/DHCP/L3/firewall, а 109 — за DNS/VPN/PBR/remote-access;
- guest network имеет один active subnet, один gateway и один management IP на гостя;
- active subnet/gateway хранятся только в `guests/defaults.yaml`;
- management IP вычисляется по VMID; отдельный IP хранится только как optional override;
- текущая central сеть — `192.168.0.0/16`, gateway `192.168.1.1`;
- будущая MAIN — `10.0.0.0/16`, gateway `10.0.0.1`;
- IOT `10.20.0.0/16`, CAMERAS `10.30.0.0/16`, REMOTE-VPN `10.60.0.0/16`;
- `home.arpa` — внутренний DNS-домен, `.local` — mDNS;
- SmartDNS — основной DNS-движок; AdGuard Home опционален; sing-box используется точечно;
- LXC defaults хранят family selector `local:vztmpl/debian-13-standard`, а конкретный archive определяется runtime;
- deployer получает ограниченный bootstrap-интерфейс только после готовности management SSH;
- `bootstrap` не является произвольным списком пакетов или Docker workloads;
- Ansible на 311 — штатный повторяемый deploy по SSH и работает контейнеризированно через Execution Environment; Semaphore — необязательный UI;
- `rootfs/` хранит только управляемые проектом файлы и не включает persistent data;
- backup: SMB/CIFS как основная внешняя копия + USB как дополнительная; для критичных систем базово 7 daily + 4 weekly и restore-test не реже раза в три месяца;
- запланированный VMID не означает обязательное немедленное создание гостя.

Статусы конкретных гостей ведутся в `guest.yaml` и при необходимости `STATUS.md`.
