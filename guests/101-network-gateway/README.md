# 101 — network-gateway

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

## Размещение конфигурации

Все файлы, которые должны оказаться внутри VM, хранятся только под `rootfs/` по реальному пути назначения. Старые верхнеуровневые каталоги `dns/`, `vpn/`, `routing/`, `nftables/` больше не используются.

Примеры целевых путей:

```text
rootfs/etc/nftables.conf
rootfs/etc/systemd/system/...
rootfs/opt/network-gateway/adguard-home/...
rootfs/opt/network-gateway/smartdns/...
```

VPN-секреты и приватные ключи в Git не добавляются. Реальные файлы `rootfs/` создаются только при появлении фактической конфигурации, без фиктивных заглушек.
