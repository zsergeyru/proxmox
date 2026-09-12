# Home Proxmox Infrastructure

Репозиторий конфигурации и документации домашней серверной инфраструктуры на Proxmox VE.

## Аппаратная платформа

- Intel N150 mini-PC
- 16 GB RAM
- M.2 SSD 512 GB
- Proxmox VE

## Основной принцип

Репозиторий организован вокруг гостей Proxmox: всё, что относится к конкретной VM/LXC, хранится в `guests/<VMID>-<name>/`. Общие решения, правила и архитектура находятся в `docs/`.

## Целевая карта гостей

| ID | Тип | Имя | Назначение |
|---:|---|---|---|
| 100 | VM | `network-gateway` | DNS, VPN, PBR, routing, nftables |
| 200 | LXC | `ha-main` | основной Home Assistant новой квартиры |
| 201 | LXC | `ha-test` | тестовый Home Assistant |
| 202 | LXC | `ha-flat2` | Home Assistant второй квартиры при необходимости |
| 210 | LXC | `automation-services` | MQTT, Zigbee2MQTT, ESPHome Dashboard |
| 300 | LXC | `app-services` | Homarr, Git, CI и прочие приложения |
| 320 | VM | `ai-control` | Hermes, MCP, AI-управление инфраструктурой |
| 330 | LXC | `ai-services` | общий STT/TTS API, Whisper и TTS |
| 400 | LXC | `monitoring` | Uptime Kuma и стек мониторинга |
| 500 | VM/LXC | `frigate` | видеонаблюдение и детекция |

Существующий HAOS, перенесённый с Raspberry Pi, пока считается legacy-системой и не перенумеровывается только ради схемы.

## Структура репозитория

```text
proxmox/
├── README.md
├── docs/                 # общая архитектура и решения
├── host/pve/             # конфигурация самого Proxmox-хоста
├── guests/               # VM/LXC: документация и конфигурация по гостям
├── ansible/              # inventory, roles, playbooks
├── scripts/              # повторяемые служебные операции
├── templates/            # шаблоны VM/LXC и конфигураций
└── archive/              # устаревшие материалы, оставленные для истории
```

## Главные документы

- [`docs/architecture.md`](docs/architecture.md) — общий паспорт Proxmox-платформы.
- [`docs/vmid-plan.md`](docs/vmid-plan.md) — правила VMID/CTID.
- [`docs/ai-control.md`](docs/ai-control.md) — архитектура AI-управления.
- [`docs/local-ai.md`](docs/local-ai.md) — локальные модели и inference.
- [`guests/320-ai-control/README.md`](guests/320-ai-control/README.md) — детальная текущая документация Hermes/MCP.

## Правило для конфигурации

Git должен быть источником истины для повторяемой конфигурации. Секреты, токены и пароли в репозиторий не добавлять.
