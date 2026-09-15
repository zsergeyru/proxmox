# Сеть Proxmox

Этот документ — источник истины для общей IPv4/VLAN/VPN-архитектуры. Фактическое состояние PVE хранится в [`../host/pve/network/README.md`](../host/pve/network/README.md), детали VM 109 — в [`../guests/109-network-gateway/README.md`](../guests/109-network-gateway/README.md).

## Базовые принципы

- у Proxmox одна физическая Ethernet-карта `nic0`;
- PVE не является неявным маршрутизатором между сегментами;
- до переезда VLAN не нужны;
- после переезда `nic0` используется как trunk через VLAN-aware `vmbr0`;
- OpenWrt edge-router отвечает за WAN, VLAN, DHCP и базовый L3/firewall;
- `109-network-gateway` отвечает за DNS, исходящие VPN, PBR, remote-access VPN и связанные сервисы;
- отказ Proxmox/109 не должен отключать обычный интернет и базовую маршрутизацию квартиры.

## Текущая квартира

Сейчас Keenetic остаётся WAN/edge-router. PVE и гости находятся в одной плоской LAN:

```text
LAN:      192.168.0.0/16
PVE:      192.168.1.11/16
gateway:  192.168.1.1
bridge:   vmbr0 → nic0
```

Central guest network в `guests/defaults.yaml`:

```yaml
defaults:
  network:
    bridge: vmbr0
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
    addressing: vmid
```

## Целевая квартира

Физическая модель:

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

Серверы пока остаются в MAIN; отдельный server VLAN не вводится.

## Management IP

Обычный адрес не хранится в каждом `guest.yaml`. Он вычисляется из active central subnet и VMID:

```text
VMID XYZ + A.B.0.0/16
→ A.B.X.YZ
```

Текущие примеры:

```text
109 → 192.168.1.9
211 → 192.168.2.11
301 → 192.168.3.1
311 → 192.168.3.11
321 → 192.168.3.21
331 → 192.168.3.31
401 → 192.168.4.1
```

После смены central subnet на `10.0.0.0/16` те же VMID автоматически дают:

```text
109 → 10.0.1.9
211 → 10.0.2.11
301 → 10.0.3.1
311 → 10.0.3.11
321 → 10.0.3.21
331 → 10.0.3.31
401 → 10.0.4.1
```

Для исключения допустим `network.ipv4.address` override в конкретном `guest.yaml`. Он хранит только IPv4 без prefix.

## Один IP и один gateway

Архитектура больше не использует:

```text
current/target address pair
mode=current/dual/target
два IP одновременно
default_gateway selector
```

В каждый момент времени у гостя одна штатная Proxmox network-конфигурация, один management IP и один default gateway.

Это одинаково соответствует VM и LXC и не требует второго механизма конфигурирования сети внутри гостевой ОС.

## Миграция `192.168 → 10.0`

Перед переключением новый gateway `10.0.0.1` и L2-доступ к новой сети должны быть готовы.

В Git меняются только central values:

```yaml
defaults:
  network:
    subnet: 10.0.0.0/16
    gateway: 10.0.0.1
```

Обычные `guest.yaml` не изменяются.

Будущий migration script должен применять новый desired state по одному гостю:

```text
preflight нового gateway/L2
→ guest N
   вычислить новый IP из VMID
   изменить IP + gateway штатным механизмом Proxmox
   применить/reboot
   проверить доступ по новому IP
→ только после успеха перейти к guest N+1
→ STOP при первой ошибке
```

Такой процесс уменьшает blast radius и позволяет явно восстановить конкретного гостя, не переключая всю инфраструктуру одновременно.

Гость с явным IP override требует отдельного внимания: при смене subnet override должен быть вручную приведён к новой сети. Validator блокирует override вне active central subnet.

## DNS

Постоянные сервисы используют `home.arpa`; `.local` остаётся для mDNS.

На 109 работает SmartDNS. DNS не является механизмом маршрутизации сам по себе: выбор ISP/VPN выполняется через nftables/PBR.

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

OpenWrt остаётся базовым default gateway VLAN.

## Remote-access VPN

Основной удалённый VPN завершается на 109:

```text
REMOTE-VPN → 10.60.0.0/16
```

На физическом роутере допускается отдельный ограниченный аварийный VPN, чтобы административный доступ не зависел от Proxmox.

## IPv6

IPv6 включается позднее как dual stack. DHCPv6-PD, RA и firewall локальных VLAN принадлежат OpenWrt. NAT66 по умолчанию не используется.

## Критерий отказоустойчивости

При остановке Proxmox или VM 109 должны сохраняться:

```text
локальный L3 между разрешёнными VLAN
обычный WAN-доступ
DHCP
базовый firewall
```

Могут быть недоступны только сознательно вынесенные на 109 функции: SmartDNS, специальные VPN/PBR, remote-access VPN и mDNS reflection.
