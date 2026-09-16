# План VMID / CTID

**Type:** Specification  
**Status:** Active  
**Source of truth:** Yes — для функциональной нумерации VMID/CTID и VMID-based addressing convention; фактический deploy desired state конкретного guest остаётся в `guest.yaml`.

Функциональная нумерация применяется только к новым VM/LXC. VMID не определяет startup order и не является причиной перенумеровывать уже работающие системы.

| Диапазон | Назначение |
|---|---|
| `100–199` | сеть и базовая инфраструктура |
| `200–299` | Home Assistant и автоматика |
| `300–399` | прикладные, DevOps- и AI-сервисы |
| `400–499` | мониторинг и обслуживание |
| `500–599` | видео и камеры |
| `600–699` | временные и экспериментальные VM/LXC |
| `900–999` | legacy/служебные объекты |

## Management IP

Для управляемых инфраструктурных VM/LXC используется детерминированная VMID-формула.

Active subnet хранится только в `guests/defaults.yaml`:

```yaml
defaults:
  network:
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
    addressing: vmid
```

Правило:

```text
VMID XYZ + A.B.0.0/16
→ A.B.X.YZ
```

При текущей сети:

```text
109 → 192.168.1.9
201 → 192.168.2.1
202 → 192.168.2.2
203 → 192.168.2.3
211 → 192.168.2.11
301 → 192.168.3.1
311 → 192.168.3.11
321 → 192.168.3.21
331 → 192.168.3.31
401 → 192.168.4.1
501 → 192.168.5.1
```

Если central subnet будет изменён на `10.0.0.0/16`, те же VMID автоматически дадут:

```text
109 → 10.0.1.9
201 → 10.0.2.1
202 → 10.0.2.2
203 → 10.0.2.3
211 → 10.0.2.11
301 → 10.0.3.1
311 → 10.0.3.11
321 → 10.0.3.21
331 → 10.0.3.31
401 → 10.0.4.1
501 → 10.0.5.1
```

В обычном `guest.yaml` IP не хранится.

Осознанное исключение задаётся через:

```yaml
network:
  ipv4:
    address: 192.168.8.50
```

без `/prefix`. Validator предупреждает, если override не соответствует VMID-формуле.

## Proxmox host

Сам PVE не обязан следовать VMID-формуле:

```text
current observed PVE → 192.168.1.11/16
target PVE           → 10.0.0.10/16
```

## Шлюзы

Текущий central gateway:

```text
192.168.1.1
```

После переезда MAIN gateway:

```text
10.0.0.1
```

В новой квартире OpenWrt владеет L3-шлюзами VLAN:

```text
MAIN / native     → 10.0.0.1/16
IOT / VLAN 20     → 10.20.0.1/16
CAMERAS / VLAN 30 → 10.30.0.1/16
```

`109-network-gateway` предоставляет DNS, VPN, PBR и remote-access VPN, но не является обязательным L3 gateway локальных VLAN.

REMOTE-VPN использует:

```text
10.60.0.0/16
```

## Миграция

Две адресные стадии внутри каждого manifest больше не хранятся.

Переезд сети выполняется изменением central `subnet` и `gateway` в `guests/defaults.yaml`, после чего migration/deploy tool последовательно приводит реальные VM/LXC к новому desired state.

## Текущий план

- `100` — `haos` (VM, **active production**, исторический VMID сохраняется)
- `109` — `network-gateway` (VM)
- `201` — `ha-main` (LXC, только возможная будущая миграция с VM 100)
- `202` — `ha-test` (LXC, при необходимости)
- `203` — `ha-flat2` (LXC, при необходимости)
- `211` — `automation-services` (LXC)
- `301` — `ai-control` (VM, целевой control plane)
- `311` — `dev-services` (LXC)
- `320` — `ai-control` (VM, текущий bootstrap до перехода на 301)
- `321` — `app-services` (LXC)
- `331` — `ai-services` (LXC)
- `401` — `monitoring` (LXC, при практической необходимости)
- `411` — резерв под `backup-services`, если появится отдельная роль
- `501` — `frigate` (VM/LXC после теста)

Запланированный VMID не означает обязательное создание гостя. Guest-validator специально не читает этот документ: deploy source of truth — `guests/defaults.yaml` и существующие `guest.yaml`.
