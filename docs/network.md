# Сеть Proxmox

Этот документ — источник истины для общей IPv4/VLAN/VPN-архитектуры. Фактическое состояние PVE хранится в [`../host/pve/network/README.md`](../host/pve/network/README.md), детали VM 101 — в [`../guests/101-network-gateway/README.md`](../guests/101-network-gateway/README.md).

## Базовые принципы

- у Proxmox одна физическая Ethernet-карта `nic0`;
- PVE не является неявным маршрутизатором между сегментами;
- до переезда VLAN не нужны;
- после переезда `nic0` используется как trunk через VLAN-aware `vmbr0`;
- физический OpenWrt edge-router отвечает за WAN, локальные VLAN, DHCP и базовый L3/firewall;
- `101-network-gateway` отвечает за DNS, исходящие VPN, PBR, remote-access VPN и связанные сервисы;
- отказ Proxmox/101 не должен отключать обычный интернет и базовую маршрутизацию квартиры.

Подробное решение о границе ролей: [`../guests/101-network-gateway/decisions/001-edge-router-boundary.md`](../guests/101-network-gateway/decisions/001-edge-router-boundary.md).

## Этап 1 — текущая квартира

Сейчас Keenetic остаётся WAN/edge-router. Компьютеры, Proxmox и VM/LXC находятся в одной плоской LAN без VLAN.

Наблюдаемая сеть PVE:

```text
LAN:      192.168.0.0/16
PVE:      192.168.1.11/16
gateway:  192.168.1.1
bridge:   vmbr0 → nic0
```

`101-network-gateway` разворачивается уже на этом этапе в той же LAN:

```text
101:      192.168.1.32/16
gateway:  192.168.1.1
```

На 101 запускаются SmartDNS, VPN/PBR, remote-access VPN и сопутствующие сервисы. Keenetic продолжает обеспечивать обычный выход в интернет независимо от доступности VM 101.

## Этап 2 — новая квартира

Целевая физическая модель:

```text
Internet
   │
OpenWrt edge-router
   │
managed switch / VLAN
   ├── MAIN / native
   ├── IOT / VLAN 20
   ├── CAMERAS / VLAN 30
   └── trunk → Proxmox nic0 → VLAN-aware vmbr0
```

Сегменты:

| Сегмент | VLAN | Подсеть | L3 gateway |
|---|---:|---|---|
| MAIN / trusted | native | `10.0.0.0/16` | `10.0.0.1` OpenWrt |
| IOT | 20 | `10.20.0.0/16` | `10.20.0.1` OpenWrt |
| CAMERAS | 30 | `10.30.0.0/16` | `10.30.0.1` OpenWrt |
| REMOTE-VPN | tunnel | `10.60.0.0/16` | VPN endpoint on 101 |

MAIN — специальный native/untagged сегмент; VLAN 0 не используется. Серверы остаются в MAIN, отдельный server VLAN пока не нужен.

Внутри VLAN третий октет можно использовать для логической группировки устройств без создания дополнительных VLAN.

## Серверные management IP

Для обычных VM/LXC действует правило из [`vmid-plan.md`](vmid-plan.md):

```text
VMID XYZ → 10.0.X.YZ/16
```

Например:

```text
PVE  → 10.0.0.10/16
211  → 10.0.2.11/16
301  → 10.0.3.1/16
311  → 10.0.3.11/16
321  → 10.0.3.21/16
401  → 10.0.4.1/16
```

`101` — исключение: его адрес выбирается по сетевой роли. Адреса `10.0.0.1`, `10.20.0.1`, `10.30.0.1` принадлежат OpenWrt, а не VM 101.

## Переход на 10.0.0.0/16

Будущую MAIN-адресацию можно проверить до переезда на той же физической LAN.

На текущем Keenetic временно добавляется адрес:

```text
10.0.0.1/16
```

После проверки NAT/маршрутизации новые серверы могут сразу получать постоянные `10.0.X.YZ/16`. Для совместимости им при необходимости временно оставляется второй `192.168.x.x/16` адрес.

Правила миграции:

- default gateway на сервере всегда один;
- сначала добавить и проверить новый IP;
- затем перевести default gateway;
- legacy-IP удалить после проверки зависимостей;
- в новой квартире `10.0.0.1` переходит от временной реализации к OpenWrt без массовой смены серверных IP.

## DNS

Постоянные сервисы используют `home.arpa`; `.local` остаётся для mDNS. Подробности — [`dns.md`](dns.md).

Клиенты могут использовать SmartDNS на 101. DNS не является механизмом маршрутизации сам по себе: фактический выбор ISP/VPN выполняется через nftables/PBR.

## Policy routing и VPN

На 101:

```text
SmartDNS / правила доменов
        ↓
nftables sets + fwmark
        ↓
ip rule / routing tables
        ├── обычный WAN
        ├── AmneziaWG
        └── другие VPN/outbound
```

OpenWrt остаётся базовым default gateway VLAN. Способ передачи выбранного трафика через 101 выбирается при внедрении PBR и должен иметь простой bypass без routing loop.

## Remote-access VPN

Основной удалённый VPN завершается на 101:

```text
REMOTE-VPN → 10.60.0.0/16
```

Удалённые клиенты должны получать домашний DNS и доступ только к разрешённым сегментам. Поддерживаются full-tunnel и split-tunnel профили.

На физическом роутере допускается отдельный ограниченный аварийный VPN, чтобы административный доступ не зависел от Proxmox.

## IPv6

IPv6 включается позднее как dual stack. Базовые DHCPv6-PD, Router Advertisement и firewall локальных VLAN принадлежат edge-router/OpenWrt. NAT66 по умолчанию не используется. Детали — [`ipv6.md`](ipv6.md).

## Ключевой критерий отказоустойчивости

При остановке Proxmox или VM 101 должны сохраняться:

```text
локальный L3 между разрешёнными VLAN
обычный WAN-доступ
DHCP
базовый firewall
```

Могут временно быть недоступны только сознательно вынесенные на 101 функции: SmartDNS, специальные VPN/PBR, remote-access VPN и mDNS reflection.
