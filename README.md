# Home Proxmox Infrastructure

Репозиторий конфигурации, кода и документации домашней серверной инфраструктуры на Proxmox VE.

## Аппаратная платформа

- Intel N150 mini-PC
- 16 GB RAM
- M.2 SSD 512 GB
- Proxmox VE

Фактическое состояние работающего хоста `pve` хранится в [`host/pve/README.md`](host/pve/README.md).

## Основной принцип

Репозиторий организован вокруг гостей Proxmox: всё, что относится к конкретной VM/LXC, хранится в `guests/<VMID>-<name>/`. Общие решения, правила и архитектура находятся в `docs/`.

По умолчанию используется один monorepo `proxmox`. Конфигурация гостя, системные файлы, Docker Compose, скрипты и собственный инфраструктурный код хранятся рядом с соответствующим гостем. Отдельный Git-репозиторий создаётся только для действительно самостоятельного компонента с отдельным жизненным циклом.

VM/LXC не обязаны клонировать весь репозиторий. Управляющий контур получает только описание нужного гостя и синхронизирует содержимое его `rootfs/` в корень `/` гостевой ОС.

## Целевая карта гостей

Актуальная нумерация определяется только файлом [`docs/vmid-plan.md`](docs/vmid-plan.md).

| ID | Тип | Имя | Назначение |
|---:|---|---|---|
| 101 | VM | `network-gateway` | DNS, VPN, PBR, routing, nftables |
| 201 | LXC | `ha-main` | основной Home Assistant новой квартиры |
| 202 | LXC | `ha-test` | тестовый Home Assistant |
| 203 | LXC | `ha-flat2` | Home Assistant второй квартиры при необходимости |
| 211 | LXC | `automation-services` | MQTT, Zigbee2MQTT, ESPHome Dashboard |
| 301 | VM | `ai-control` | целевой AI-контур управления инфраструктурой |
| 311 | LXC | `dev-services` | Gitea/Gogs, Jenkins и DevOps-сервисы |
| 321 | LXC | `app-services` | Homarr и прочие прикладные сервисы |
| 331 | LXC | `ai-services` | общий STT/TTS API, Whisper и TTS |
| 401 | LXC | `monitoring` | Uptime Kuma и стек мониторинга |
| 501 | VM/LXC | `frigate` | видеонаблюдение и детекция |

Текущая production VM `100 HAOS` сохраняется как legacy-гость и описана в [`guests/100-haos/`](guests/100-haos/). `320-ai-control` сохраняется как текущий bootstrap-узел до развёртывания и проверки целевой `301-ai-control`. Существующие legacy/bootstrap VM не перенумеровываются только ради новой схемы.

## Структура репозитория

```text
proxmox/
├── README.md
├── docs/                 # общая архитектура, правила и решения
├── host/pve/             # конфигурация самого Proxmox-хоста
├── guests/               # VM/LXC: паспорт гостя и дерево его файлов
├── ansible/              # общие inventory, roles и playbooks
├── scripts/              # повторяемые операции общего назначения
├── templates/            # шаблоны VM/LXC и конфигураций
└── archive/              # устаревшие материалы для истории
```

Типовая структура гостя:

```text
guests/<VMID>-<name>/
├── README.md
├── guest.yaml
├── decisions/            # при необходимости
├── STATUS.md             # при необходимости
└── rootfs/
```

`guest.yaml` описывает объект VM/LXC в Proxmox: VMID, тип, state, ресурсы, сеть, автозапуск и способ управления.

`rootfs/` повторяет корневую файловую систему гостя. Например:

```text
guests/101-network-gateway/rootfs/etc/nftables.conf
→ /etc/nftables.conf
```

Отдельных универсальных каталогов `files/` и `services/` у гостя нет. Приложения, Compose-файлы, MCP, агенты и системные конфиги размещаются внутри `rootfs/` по реальному пути назначения.

## Источники истины

При расхождении документов использовать такой приоритет:

1. [`docs/vmid-plan.md`](docs/vmid-plan.md) — VMID/CTID и целевая карта гостей.
2. [`guests/README.md`](guests/README.md) — формат `guest.yaml`, `rootfs/` и правила каталогов гостей.
3. ADR в `guests/<guest>/decisions/` — принятые архитектурные решения конкретного гостя.
4. README конкретного гостя — его назначение и эксплуатационные особенности.
5. [`docs/architecture.md`](docs/architecture.md) — сводная архитектура без дублирования низкоуровневой конфигурации.

Наблюдаемое текущее состояние хоста хранится в `host/pve/`, а временный observed drift конкретного гостя — в его `STATUS.md`.

## Главные документы

- [`docs/architecture.md`](docs/architecture.md) — сводная архитектура Proxmox-платформы.
- [`docs/vmid-plan.md`](docs/vmid-plan.md) — правила VMID/CTID.
- [`docs/ai-control.md`](docs/ai-control.md) — архитектура AI-управления.
- [`docs/local-ai.md`](docs/local-ai.md) — локальные модели и inference.
- [`docs/network.md`](docs/network.md) — сеть Proxmox и роль `network-gateway`.
- [`docs/dns.md`](docs/dns.md) — `home.arpa`, локальный DNS, mDNS `.local` и reflection между VLAN.
- [`docs/ipv6.md`](docs/ipv6.md) — dual stack и IPv6.
- [`docs/apartment-infrastructure.md`](docs/apartment-infrastructure.md) — граница между этим репозиторием и проектом квартиры.
- [`guests/320-ai-control/README.md`](guests/320-ai-control/README.md) — текущий bootstrap Hermes/MCP.

## Общие правила

- Git является источником истины для повторяемой конфигурации и собственного инфраструктурного кода;
- на гостя разворачивается только его собственное содержимое `rootfs/`;
- конфигурации других VM/LXC не копируются внутрь `ai-control`;
- Ansible допускается как инструмент поверх SSH, но не является обязательным слоем;
- отдельный deploy-MCP не требуется: Proxmox MCP управляет VM/LXC, SSH — содержимым гостевых ОС;
- секреты, токены, пароли и приватные ключи в Git не добавляются.
