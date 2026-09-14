# Сеть Proxmox

Этот документ — источник истины для общей IPv4/VLAN/VPN-архитектуры. Фактическое состояние PVE хранится в [`../host/pve/network/README.md`](../host/pve/network/README.md), детали VM 109 — в [`../guests/109-network-gateway/README.md`](../guests/109-network-gateway/README.md).

## Базовые принципы

- у Proxmox одна физическая Ethernet-карта `nic0`;
- PVE не является неявным маршрутизатором между сегментами;
- до переезда VLAN не нужны;
- после переезда `nic0` используется как trunk через VLAN-aware `vmbr0`;
- физический OpenWrt edge-router отвечает за WAN, локальные VLAN, DHCP и базовый L3/firewall;
- `109-network-gateway` отвечает за DNS, исходящие VPN, PBR, remote-access VPN и связанные сервисы;
- отказ Proxmox/109 не должен отключать обычный интернет и базовую маршрутизацию квартиры.

Подробное решение о границе ролей: [`../guests/109-network-gateway/decisions/001-edge-router-boundary.md`](../guests/109-network-gateway/decisions/001-edge-router-boundary.md).

## Этап 1 — текущая квартира

Сейчас Keenetic остаётся WAN/edge-router. Компьютеры, Proxmox и VM/LXC находятся в одной плоской LAN без VLAN.

Наблюдаемая сеть PVE:

```text
LAN:      192.168.0.0/16
PVE:      192.168.1.11/16
gateway:  192.168.1.1
bridge:   vmbr0 → nic0
```

`109-network-gateway` разворачивается в той же LAN:

```text
109:      192.168.1.9/16
gateway:  192.168.1.1
```

На 109 запускаются SmartDNS, VPN/PBR, remote-access VPN и сопутствующие сервисы. Keenetic продолжает обеспечивать обычный выход в интернет независимо от доступности VM 109.

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
| REMOTE-VPN | tunnel | `10.60.0.0/16` | VPN endpoint on 109 |

MAIN — специальный native/untagged сегмент; VLAN 0 не используется. Серверы остаются в MAIN, отдельный server VLAN пока не нужен.

## Серверные management IP

Для VM/LXC действует единое правило из [`11-vmid-plan.md`](11-vmid-plan.md):

```text
current: VMID XYZ → 192.168.X.YZ/16
target:  VMID XYZ → 10.0.X.YZ/16
```

Например:

```text
109 → 192.168.1.9  → 10.0.1.9
211 → 192.168.2.11 → 10.0.2.11
301 → 192.168.3.1  → 10.0.3.1
311 → 192.168.3.11 → 10.0.3.11
321 → 192.168.3.21 → 10.0.3.21
401 → 192.168.4.1  → 10.0.4.1
```

Исключения для `network-gateway` больше нет: VMID `109` выбран именно так, чтобы сетевой сервер подчинялся общей формуле и не конфликтовал с gateway `.1`.

## Переход на 10.0.0.0/16

Будущую MAIN-адресацию можно проверить до переезда на той же физической LAN. На текущем Keenetic временно добавляется адрес `10.0.0.1/16`.

В manifests planned-гостей заранее записаны оба состояния — `network.current` и `network.target`. Это не означает два одновременных default gateway.

Правила миграции:

- default gateway на сервере всегда один;
- сначала добавить и проверить новый IP;
- затем перевести default gateway;
- legacy-IP удалить после проверки зависимостей;
- в новой квартире `10.0.0.1` переходит от временной реализации к OpenWrt без массовой смены серверных IP.

## DNS

Постоянные сервисы используют `home.arpa`; `.local` остаётся для mDNS. Подробности — [`41-dns.md`](41-dns.md).

Клиенты могут использовать SmartDNS на 109. DNS не является механизмом маршрутизации сам по себе: фактический выбор ISP/VPN выполняется через nftables/PBR.

## Policy routing и VPN

На 109:

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

OpenWrt остаётся базовым default gateway VLAN. Способ передачи выбранного трафика через 109 выбирается при внедрении PBR и должен иметь простой bypass без routing loop.

## Remote-access VPN

Основной удалённый VPN завершается на 109:

```text
REMOTE-VPN → 10.60.0.0/16
```

Удалённые клиенты должны получать домашний DNS и доступ только к разрешённым сегментам. Поддерживаются full-tunnel и split-tunnel профили.

На физическом роутере допускается отдельный ограниченный аварийный VPN, чтобы административный доступ не зависел от Proxmox.

## IPv6

IPv6 включается позднее как dual stack. Базовые DHCPv6-PD, Router Advertisement и firewall локальных VLAN принадлежат edge-router/OpenWrt. NAT66 по умолчанию не используется. Детали — [`42-ipv6.md`](42-ipv6.md).

## Ключевой критерий отказоустойчивости

При остановке Proxmox или VM 109 должны сохраняться:

```text
локальный L3 между разрешёнными VLAN
обычный WAN-доступ
DHCP
базовый firewall
```

Могут временно быть недоступны только сознательно вынесенные на 109 функции: SmartDNS, специальные VPN/PBR, remote-access VPN и mDNS reflection.
