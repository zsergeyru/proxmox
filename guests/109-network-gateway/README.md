# 109 — network-gateway

**Тип:** VM  
**ОС:** Debian 13  
**Назначение:** DNS, исходящие VPN, policy-based routing, remote-access VPN и сетевые сервисы, которые удобнее держать на Linux-сервере.

Общая сеть и VLAN описаны в [`../../docs/40-network.md`](../../docs/40-network.md). Граница ответственности физического OpenWrt и этой VM зафиксирована в [`decisions/001-edge-router-boundary.md`](decisions/001-edge-router-boundary.md).

## Что делает 109

На уровне ОС VM:

- `nftables`;
- `ip rule` и отдельные routing tables;
- fwmark/PBR;
- AmneziaWG/WireGuard и другие исходящие VPN;
- основной remote-access VPN;
- health-check/watchdog;
- при необходимости mDNS reflector между явно разрешёнными сегментами.

В Docker допускаются:

- SmartDNS — основной DNS-сервис;
- AdGuard Home — только опциональный frontend для статистики/фильтрации;
- sing-box — только для proxy/outbound-сценариев, где обычной L3-маршрутизации недостаточно.

Маршрутизация VPN/PBR принадлежит ОС VM, а не Docker networking.

## DNS/PBR

Принятая схема:

```text
SmartDNS
   ↓
адреса назначения / nftables sets
   ↓
fwmark
   ↓
ip rule / routing tables
   ├── обычный WAN
   ├── AmneziaWG
   └── другие VPN/outbound
```

SmartDNS поддерживает обычные upstream DNS, DoH/DoT и группы доменов. `keen-pbr` внутри VM не используется.

Постоянный внутренний домен — `home.arpa`. `.local` используется только для mDNS. Полная DNS-модель: [`../../docs/41-dns.md`](../../docs/41-dns.md).

## Адресация

VM подчиняется тому же правилу VMID → IP, что и остальные серверы:

```text
current: 192.168.1.9/16
gateway: 192.168.1.1

target:  10.0.1.9/16
gateway: 10.0.0.1

bridge: vmbr0
```

До переезда Keenetic остаётся обычным edge-router и default gateway. После переезда физический OpenWrt владеет локальными L3 gateway VLAN, а 109 остаётся специализированным DNS/VPN/PBR-узлом.

Остановка 109 не должна отключать обычный WAN и базовую маршрутизацию квартиры.

## Remote-access VPN

Основная tunnel-сеть:

```text
10.60.0.0/16
```

Удалённые доверенные клиенты получают домашний DNS и доступ только к разрешённым сегментам. Поддерживаются split-tunnel и full-tunnel профили.

Ограниченный аварийный VPN на физическом edge-router допускается отдельно, чтобы доступ к инфраструктуре не зависел от VM 109.

## IPv6

Базовые DHCPv6-PD, Router Advertisement и firewall локальных VLAN принадлежат OpenWrt. VM 109 обрабатывает IPv6 только в пределах собственных DNS/VPN/PBR-функций, если отдельный сценарий не потребует иного.

## Ресурсы

```text
vCPU: 2
RAM: 2 GiB
Disk: 16 GiB начально
NIC: VirtIO
QEMU Guest Agent: enabled
Ballooning: пока не использовать
```

## Размещение конфигурации

Все файлы гостевой ОС хранятся только под `rootfs/` по реальному пути назначения:

```text
rootfs/etc/nftables.conf
rootfs/etc/systemd/system/...
rootfs/etc/wireguard/...
rootfs/opt/network-gateway/smartdns/...
```

Секреты VPN, private keys, preshared keys и пароли в Git не добавляются.
