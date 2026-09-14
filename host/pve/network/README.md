# Host network

## Фактическое состояние на 2026-09-12

```text
vmbr0
address: 192.168.1.11/16
gateway: 192.168.1.1
bridge-port: nic0
STP: off
forward delay: 0
```

Маршруты:

```text
default via 192.168.1.1 dev vmbr0
192.168.0.0/16 dev vmbr0 src 192.168.1.11
```

Физический uplink:

```text
interface: nic0
controller: Realtek RTL8111/8168 family
speed class: 1 GbE
driver: r8169
```

На сервере используется одна физическая Ethernet-карта. Intel Wi-Fi AX101 (`wlo1`) выключен и в bridge не используется.

В `/etc/network/interfaces` пока присутствует остаточная строка `iface nic1 inet manual`, но интерфейс `nic1` фактически отсутствует. После контрольной проверки её нужно удалить.

## DNS хоста

```text
search home.arpa
nameserver 192.168.1.1
```

Сейчас PVE использует Keenetic как DNS. Общая DNS-архитектура: [`../../../docs/41-dns.md`](../../../docs/41-dns.md).

## Этап 1 — текущая квартира

PVE подключён одним `nic0` к обычной LAN Keenetic без VLAN. Компьютеры, PVE и VM/LXC находятся в одном L2.

`109-network-gateway` разворачивается в этой же LAN по адресу `192.168.1.9/16`. Keenetic остаётся edge-router и default gateway, поэтому остановка 109 не должна отключать обычный интернет.

## Этап 2 — новая квартира

Тот же `nic0` используется как trunk через VLAN-aware `vmbr0`.

Физический OpenWrt отвечает за:

- WAN;
- termination VLAN;
- DHCP;
- базовую L3/inter-VLAN маршрутизацию;
- firewall локальных сегментов.

`109-network-gateway` отвечает за SmartDNS, VPN/PBR, remote-access VPN и дополнительные сетевые сервисы.

Подробности и адресация находятся в [`../../../docs/40-network.md`](../../../docs/40-network.md).

## Целевая миграция PVE

Постоянный management IP PVE:

```text
10.0.0.10/16
gateway 10.0.0.1
```

До миграции фактический адрес остаётся:

```text
192.168.1.11/16
gateway 192.168.1.1
```

Будущий адрес можно добавить и проверить заранее, а старый удалить только после подтверждения DNS, маршрутизации и административного доступа.

## Принцип отказоустойчивости

PVE не должен быть маршрутизатором квартиры. Перезапуск PVE/VM 109 не должен отключать базовые DHCP, WAN и межсегментную маршрутизацию, которыми владеет физический edge-router.
