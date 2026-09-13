# Home Proxmox Infrastructure

Репозиторий конфигурации, кода и документации домашней серверной инфраструктуры на Proxmox VE.

## Аппаратная платформа

- Intel N150 mini-PC
- 16 GB RAM
- M.2 SSD 512 GB
- Proxmox VE

Фактическое состояние работающего хоста `pve` хранится в [`host/pve/README.md`](host/pve/README.md).

## Основной принцип

Репозиторий организован вокруг гостей Proxmox: всё, что относится к конкретной VM/LXC, хранится в `guests/<VMID>-<name>/`. Общие решения и архитектура находятся в `docs/`.

Используется один основной monorepo `proxmox`. Отдельный Git-репозиторий создаётся только для действительно самостоятельного компонента с отдельным жизненным циклом.

VM/LXC не обязаны клонировать весь репозиторий. Повторяемое применение управляемых файлов выполняется Ansible на `311-dev-services` по SSH.

## Текущая и целевая карта гостей

Актуальная нумерация определяется только [`docs/vmid-plan.md`](docs/vmid-plan.md).

| ID | Тип | Имя | Статус/назначение |
|---:|---|---|---|
| 100 | VM | `haos` | текущий production Home Assistant |
| 109 | VM | `network-gateway` | DNS, VPN, PBR, remote-access VPN |
| 201 | LXC | `ha-main` | только возможная будущая миграция с 100 |
| 202 | LXC | `ha-test` | тестовый HA при необходимости |
| 203 | LXC | `ha-flat2` | второй HA при необходимости |
| 211 | LXC | `automation-services` | MQTT, Zigbee2MQTT, ESPHome |
| 301 | VM | `ai-control` | целевой AI control plane |
| 311 | LXC | `dev-services` | Ansible, Semaphore, Git/CI |
| 320 | VM | `ai-control` | текущий bootstrap до перехода на 301 |
| 321 | LXC | `app-services` | Homarr и прикладные сервисы |
| 331 | LXC | `ai-services` | STT/TTS API |
| 401 | LXC | `monitoring` | мониторинг при необходимости |
| 501 | VM/LXC | `frigate` | видеонаблюдение после теста |

Запланированный VMID не означает, что гостя нужно создавать заранее. Рабочая VM `100 HAOS` не мигрирует только ради красивой нумерации.

## Структура

```text
proxmox/
├── README.md
├── docs/                 # общая архитектура и решения
├── host/pve/             # фактическое состояние Proxmox-хоста
├── guests/               # паспорта VM/LXC и управляемые файлы
├── ansible/              # inventory, roles и playbooks
├── scripts/              # общие повторяемые операции
├── templates/            # базовые VM/LXC templates
└── archive/              # устаревшие материалы
```

Правила структуры гостя и `guest.yaml` находятся только в [`guests/README.md`](guests/README.md).

## Управление и deploy

```text
Proxmox MCP
→ lifecycle VM/LXC

Ansible на 311-dev-services
→ повторяемая настройка гостевых ОС по SSH

прямой SSH
→ bootstrap, диагностика и аварийные действия

Semaphore
→ необязательный web-интерфейс к Ansible
```

Для Debian-инфраструктуры используется единый административный пользователь `ops`; отдельный `infra-agent` сейчас не вводится.

## Сеть

До переезда Keenetic остаётся edge-router, а `109-network-gateway` запускается в общей LAN.

После переезда физический OpenWrt отвечает за WAN, VLAN, DHCP и базовый L3/firewall. `109` отвечает за SmartDNS, VPN/PBR, remote-access VPN и сопутствующие сетевые сервисы. Поэтому остановка Proxmox/109 не должна отключать базовый интернет и межсетевую маршрутизацию квартиры.

Источник истины: [`docs/network.md`](docs/network.md).

## Источники истины

При расхождении документов использовать такой приоритет:

1. [`docs/vmid-plan.md`](docs/vmid-plan.md) — VMID/CTID и management IP.
2. [`guests/README.md`](guests/README.md) — формат `guest.yaml`, `rootfs/` и deploy.
3. ADR в `guests/<guest>/decisions/` — принятые решения конкретного гостя.
4. профильный документ в `docs/` (`network.md`, `dns.md`, `storage-and-backup.md` и т. п.).
5. README конкретного гостя — его эксплуатационные особенности.
6. [`docs/architecture.md`](docs/architecture.md) — сводка без дублирования деталей.

Наблюдаемое состояние PVE хранится в `host/pve/`, временный drift конкретного гостя — в его `STATUS.md`.

## Главные документы

- [`docs/architecture.md`](docs/architecture.md) — сводная архитектура.
- [`docs/vmid-plan.md`](docs/vmid-plan.md) — VMID/CTID и management IP.
- [`docs/network.md`](docs/network.md) — IPv4/VLAN/VPN и граница OpenWrt ↔ 109.
- [`docs/dns.md`](docs/dns.md) — SmartDNS, `home.arpa`, mDNS.
- [`docs/ai-control.md`](docs/ai-control.md) — AI-управление.
- [`docs/storage-and-backup.md`](docs/storage-and-backup.md) — backup, retention, RPO/RTO и restore-test.
- [`templates/debian13/README.md`](templates/debian13/README.md) — базовый Debian 13 template.

## Общие правила

- Git — источник истины для повторяемой конфигурации и собственного инфраструктурного кода;
- на гость разворачивается только его собственное содержимое `rootfs/`;
- persistent data и секреты не являются `rootfs/`;
- Ansible — штатный повторяемый deploy внутри Linux-гостей;
- SSH сохраняется как базовый и аварийный административный канал;
- отдельный универсальный deploy-MCP не требуется;
- секреты, токены, пароли и private keys в Git не добавляются.
