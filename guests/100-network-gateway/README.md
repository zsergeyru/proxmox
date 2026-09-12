# 100 — network-gateway

**Тип:** VM  
**ОС:** Debian (целевая)  
**Назначение:** пользовательский сетевой шлюз, DNS, VPN и policy-based routing.

## В ОС VM

- IP forwarding;
- nftables;
- `ip rule` и routing tables;
- fwmark/PBR;
- Amnezia/AmneziaWG и другие VPN-клиенты;
- health-check/watchdog.

## В Docker

- AdGuard Home;
- SmartDNS.

Маршрутизация и VPN принадлежат ОС VM, а не Docker networking.

## Предварительные ресурсы

- vCPU: 2
- RAM: 2 GB
- Disk: 16–32 GB
- NIC: VirtIO
- Ballooning: на первом этапе не использовать

Подкаталоги `nftables/`, `routing/`, `dns/`, `vpn/` предназначены для реальных конфигураций этой VM.
