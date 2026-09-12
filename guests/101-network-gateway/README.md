# 101 — network-gateway

**Тип:** VM  
**ОС:** Debian (целевая)  
**Назначение:** пользовательский сетевой шлюз, DNS, исходящие VPN, remote-access VPN и policy-based routing.

## В ОС VM

- IP forwarding;
- nftables;
- `ip rule` и routing tables;
- fwmark/PBR;
- Amnezia/AmneziaWG и другие VPN-клиенты для исходящего трафика;
- WireGuard/AmneziaWG server для удалённого доступа;
- маршрутизация remote-access клиентов в домашние сегменты;
- health-check/watchdog.

## В Docker

- AdGuard Home;
- SmartDNS.

Маршрутизация, PBR и VPN принадлежат ОС VM, а не Docker networking.

## Remote-access VPN

Удалённые доверенные ноутбуки, компьютеры и телефоны подключаются к этой VM и получают адреса из отдельной tunnel-сети:

```text
REMOTE-VPN  10.60.0.0/16
gateway     10.60.0.1
```

Целевой профиль удалённого клиента:

- домашний DNS через `10.60.0.1` → AdGuard Home / SmartDNS;
- доступ к MAIN `10.0.0.0/16`;
- доступ к SERVERS `10.10.0.0/16`;
- доступ к IOT `10.20.0.0/16`;
- доступ к CAMERAS `10.30.0.0/16`;
- возможность использовать full-tunnel через домашний интернет-шлюз;
- возможность использовать split-tunnel только для домашних сетей;
- применение тех же PBR и исходящих VPN-политик, что и для локальных клиентов.

Основной remote-access VPN должен завершаться именно здесь, чтобы DNS, firewall, PBR и исходящие VPN оставались единым контуром. Пока внешний edge-router остаётся отдельным устройством, на нём требуется только проброс входящего UDP-порта на эту VM. После переноса WAN-маршрутизации в `network-gateway` VPN будет принимать соединения непосредственно на WAN-интерфейсе.

Дополнительный ограниченный VPN на физическом роутере можно сохранить только как аварийный административный канал на случай недоступности Proxmox или этой VM.

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
rootfs/etc/wireguard/...
rootfs/opt/network-gateway/adguard-home/...
rootfs/opt/network-gateway/smartdns/...
```

VPN-секреты, peer private keys и preshared keys в Git не добавляются. Реальные файлы `rootfs/` создаются только при появлении фактической конфигурации, без фиктивных заглушек.
