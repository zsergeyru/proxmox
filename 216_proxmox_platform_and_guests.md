# Proxmox — платформа, виртуальные машины, LXC и правила эксплуатации

Главные связанные документы:

- [201_network_equipment_plan.md](201_network_equipment_plan.md) — сводный паспорт сети;
- [203_network_segments_and_routing.md](203_network_segments_and_routing.md) — VLAN, сегменты и firewall;
- [205_server_network_and_services.md](205_server_network_and_services.md) — сервер и распределение сервисов;
- [208_network_gateway_vm.md](208_network_gateway_vm.md) — сетевая VM, DNS/VPN/PBR;
- [209_ai_control_agent.md](209_ai_control_agent.md) — AI Control Agent;
- [213_frigate_and_video_storage.md](213_frigate_and_video_storage.md) — Frigate и хранение видео.

## 1. Статус документа

> **Общий статус: 🟠 рабочий сводный паспорт Proxmox-платформы.**
>
> **🟢 Принято:** `Proxmox VE` является базовой серверной платформой квартиры; прикладные сервисы не устанавливаются непосредственно на хост Proxmox; критичные и логически самостоятельные подсистемы разделяются по VM/LXC; небольшие сервисы одного класса группируются через Docker Compose внутри соответствующего гостя.
>
> **🟢 Принято:** сеть сервера строится через VLAN-aware bridge, а сам Proxmox не должен становиться неявным маршрутизатором между сегментами.
>
> **🟢 Принято:** для новых гостей применяется функциональная нумерация VMID/CTID; `network-gateway`, Home Assistant, AI Control, общие AI-сервисы, мониторинг и Frigate логически разделены.
>
> **🟠 Рабочее:** точные CPU/RAM/storage большинства гостей, окончательный состав мониторинга, способ размещения Frigate как VM или LXC, схема резервного копирования и фактический порядок запуска.
>
> **❓ Проверить:** ресурсы `network-gateway`, нагрузку Frigate/OpenVINO, storage для видео и backup, VLAN/firewall, финальную схему remote-deploy Docker по всем управляемым гостям.

Этот файл является **единым сводным документом именно по Proxmox**. Детали отдельных подсистем остаются в профильных документах и имеют приоритет в своей области.

## 2. Аппаратная платформа

Сервер:

```text
BRENUC mini-PC
CPU: Intel N150
RAM: 16 ГБ
System SSD: M.2 512 ГБ
Дополнительный SSD: предусмотрен конструкцией
Hypervisor: Proxmox VE
```

Главный принцип использования ресурсов: не пытаться запускать все функции непосредственно на хосте и не создавать полноценную VM для каждого маленького приложения. Ресурсы распределяются между изолированными функциональными гостями.

Локальную крупную LLM на Intel N150 с 16 ГБ RAM не считать обязательной частью Proxmox-сервера. Основной inference для AI Control целево выполняется на отдельном GPU-компьютере, а сервер Proxmox содержит управляющие и инфраструктурные сервисы.

## 3. Базовые правила Proxmox

### 3.1. Что не устанавливать на хост

На самом Proxmox не размещать обычные прикладные сервисы:

- Home Assistant;
- DNS-сервисы;
- VPN-клиенты пользовательской сети;
- Gitea/Jenkins/Homarr;
- MQTT/Zigbee2MQTT/ESPHome Dashboard;
- AI-agent;
- Whisper/TTS;
- Frigate;
- monitoring stack.

На хосте остаются функции гипервизора, storage, bridge/VLAN и необходимые системные средства Proxmox.

### 3.2. Когда VM, а когда LXC

**VM** использовать там, где нужна сильная изоляция, собственный kernel/networking stack или потенциально опасный управляющий контур:

- `network-gateway`;
- `ai-control`;
- существующий legacy HAOS;
- возможно Frigate, если это потребуется для удобного доступа к аппаратным ресурсам.

**LXC** использовать для лёгких функциональных сервисов, где полноценная VM не даёт существенного преимущества:

- Home Assistant новой квартиры;
- automation-services;
- app-services;
- ai-services;
- monitoring;
- возможно Frigate после теста.

Docker Compose допускается **внутри VM/LXC**, если конкретный гость является логическим контейнером для группы приложений.

## 4. Целевая карта гостей

```text
Proxmox VE
│
├── 100  VM   network-gateway
│   ├── Linux routing / nftables / policy routing
│   ├── VPN-клиенты в ОС VM
│   └── Docker Compose
│       ├── AdGuard Home
│       └── SmartDNS
│
├── 200  LXC  ha-main
│   └── основной Home Assistant новой квартиры
│
├── 201  LXC  ha-test
│   └── тестовый Home Assistant
│
├── 202  LXC  ha-flat2
│   └── Home Assistant второй квартиры при необходимости
│
├── 210  LXC  automation-services
│   └── Docker Compose
│       ├── Mosquitto / MQTT
│       ├── Zigbee2MQTT
│       └── ESPHome Dashboard
│
├── 300  LXC  app-services
│   └── Docker Compose
│       ├── Homarr
│       ├── Gitea / Gogs
│       ├── Jenkins
│       ├── password server
│       └── torserver
│
├── 320  VM   ai-control
│   ├── AI Control Agent / Hermes
│   ├── Ansible / Git / SSH / API clients
│   └── Docker для сервисов управляющего контура
│
├── 330  LXC  ai-services
│   └── Docker Compose
│       ├── Whisper / faster-whisper — общий STT API
│       └── TTS — Kokoro / Piper или другой движок
│
├── 400  LXC  monitoring
│   └── Docker Compose
│       ├── Uptime Kuma
│       └── Grafana / Prometheus / InfluxDB / syslog при необходимости
│
├── 500  Frigate — VM или LXC после теста
│   └── видеозапись, детекция, OpenVINO/iGPU
│
└── legacy-haos
    └── существующий HAOS, перенесённый со старого Raspberry Pi
```

Точная фактическая нумерация уже существующей legacy-VM не меняется только ради унификации схемы.

## 5. Функциональная нумерация VMID / CTID

Приняты диапазоны:

| Диапазон | Назначение |
|---|---|
| `100–199` | сеть и базовая инфраструктура |
| `200–299` | Home Assistant и автоматика |
| `300–399` | прикладные, DevOps- и AI-сервисы |
| `400–499` | мониторинг и обслуживание |
| `500–599` | видео и камеры |
| `600–699` | временные и экспериментальные VM/LXC |
| `900–999` | legacy-системы |

Дополнительные резервы:

```text
310  резерв, если Homarr когда-нибудь потребуется вынести отдельно
410  резерв под отдельный backup-services
600–699  тестовые и временные гости
```

VMID/CTID не определяет порядок запуска. Startup order задаётся отдельно в Proxmox.

## 6. Сеть Proxmox

Сервер подключается одной физической NIC.

Целевая схема:

```text
NIC сервера
├── MAIN — native / untagged
├── VLAN 20 — IOT
└── VLAN 30 — CAMERAS
```

На Proxmox используется **VLAN-aware bridge**.

Принципы:

- сервер получает trunk с необходимыми VLAN;
- гостю назначаются только реально требуемые сегменты;
- Home Assistant получает доступ к MAIN/IOT/CAMERAS в нужном объёме;
- Frigate получает доступ к CAMERAS;
- `network-gateway` получает интерфейсы/сегменты, необходимые для роли пользовательского шлюза;
- Proxmox не должен автоматически маршрутизировать трафик между VLAN;
- IP forwarding и firewall включаются только там, где это осознанно требуется.

Базовые правила сегментации определяются в [203_network_segments_and_routing.md](203_network_segments_and_routing.md).

## 7. VM `network-gateway`

`network-gateway` — отдельная виртуальная машина, которая целево становится IP-шлюзом и DNS-точкой для клиентского трафика квартиры.

На уровне Linux VM:

- IP forwarding;
- `nftables`;
- `ip rule` и routing tables;
- policy routing;
- fwmark;
- VPN-интерфейсы;
- Amnezia/AmneziaWG и другие VPN-клиенты;
- DIRECT/VPN routing;
- watchdog/health-check.

В Docker внутри VM:

```text
AdGuard Home
SmartDNS
```

Главный принцип — маршрутизация и VPN принадлежат ОС VM, а не Docker networking.

Предварительные ресурсы из профильного документа:

```text
OS: Debian
vCPU: 2
RAM: 2 ГБ
Disk: 16–32 ГБ
NIC: VirtIO
Ballooning: не использовать на первом этапе
```

Ресурсы необходимо подтвердить после реального запуска.

## 8. Home Assistant и автоматика

### `legacy-haos`

Существующий Home Assistant со старого Raspberry Pi перенесён в отдельную VM HAOS. Это исключение из целевой архитектуры.

Правила:

- оставить работоспособным;
- не использовать как фундамент новой квартиры;
- не добавлять туда общесерверные сервисы;
- не делать новые системы зависимыми от legacy HAOS;
- миграцию/вывод из эксплуатации решать отдельно.

### `ha-main`, `ha-test`, `ha-flat2`

Для новой архитектуры Home Assistant разделяется по назначению:

- `ha-main` — основная новая квартира;
- `ha-test` — тест обновлений и интеграций;
- `ha-flat2` — вторая квартира при необходимости.

### `automation-services`

Отдельный LXC для инфраструктуры автоматики:

- Mosquitto/MQTT;
- Zigbee2MQTT;
- ESPHome Dashboard.

Отказ этого слоя не должен затрагивать DNS, Git, monitoring и другие несвязанные сервисы.

## 9. Прикладные сервисы

`app-services` — LXC с Docker Compose для небольших приложений общего назначения:

- Homarr;
- Gitea/Gogs;
- Jenkins;
- password server;
- torserver;
- другие некритичные приложения того же класса.

Homarr является верхним навигационным интерфейсом и должен давать ссылки/виджеты на Proxmox, Home Assistant, AI Control, сеть/DNS/VPN, мониторинг, Frigate и другие сервисы.

Homarr не заменяет специализированный интерфейс Proxmox.

## 10. AI Control и управление Proxmox

`ai-control` размещается в отдельной VM, потому что агент получает потенциально критичные полномочия.

Целевые инструменты:

```text
Hermes / AI Agent
Proxmox API client
SSH
Ansible
Git
Python
curl / jq
REST/API clients
Docker / Docker Compose
```

Основной путь управления Proxmox — **Proxmox API**. SSH к хосту использовать только там, где API недостаточно, и с ограниченными правами.

Агент должен уметь:

- получать состояние Proxmox и гостей;
- создавать VM/LXC по шаблонам;
- запускать/останавливать/перезапускать гостей;
- менять CPU/RAM/disk в разрешённых пределах;
- создавать snapshot/backup;
- читать логи и диагностику;
- проверять storage;
- обслуживать Docker Compose в разрешённых гостях;
- выполнять изменения с последующей автоматической проверкой результата.

### Уровни риска

**Уровень 1 — автоматический:** чтение состояния, логи, безопасный deploy/restart некритичных приложений.

**Уровень 2 — backup/snapshot + проверка:** критичные обновления, VPN, nftables, policy routing, persistent data.

**Уровень 3 — только явное подтверждение:** `vmbr0`, management IP/gateway Proxmox, удаление VM/LXC или backup, storage, диски, глобальный firewall, действия, способные одновременно лишить доступа Proxmox и `ai-control`.

## 11. Общий Docker deploy

Целевое развитие `ai-control` — единый механизм deploy для Docker во всех разрешённых VM/LXC, а не только во внутреннем Docker самого агента.

Логическая схема:

```text
Hermes
  │
  ├── Proxmox API → создание/изменение VM/LXC
  │
  └── remote deploy → SSH/Ansible → выбранный Docker host
                                   ├── automation-services
                                   ├── app-services
                                   ├── ai-services
                                   ├── monitoring
                                   └── network-gateway (только разрешённые app-контейнеры)
```

Для каждого Docker host должен существовать inventory и allowlist сервисов. Разрушающие действия с persistent data требуют отдельного подтверждения.

Рекомендуемый цикл deploy:

```text
проверка host
→ backup текущей конфигурации
→ обновление/создание compose
→ docker compose pull
→ docker compose up -d
→ healthcheck
→ rollback при ошибке
→ фиксация конфигурации/изменения в Git
```

`network-gateway` требует отдельной осторожности: Docker deploy не должен самовольно менять Linux routing, VPN, nftables или сетевой путь управления.

## 12. Общие AI-сервисы

Whisper/STT и TTS не размещаются внутри Hermes как его внутренние компоненты.

Для них выделяется `330 LXC ai-services`:

```text
ai-services
└── Docker Compose
    ├── Whisper / faster-whisper
    └── TTS
```

Этими API могут пользоваться:

- Hermes;
- Home Assistant;
- голосовые панели;
- Open WebUI;
- другие будущие приложения.

Перезапуск или замена AI-агента не должен останавливать общие голосовые функции.

## 13. Monitoring

`400 LXC monitoring` предназначен для наблюдения за инфраструктурой.

Минимально:

- Uptime Kuma.

Опционально:

- Grafana;
- Prometheus;
- InfluxDB;
- центральный syslog;
- NetFlow collector;
- локальный NTP.

Monitoring должен быть логически отделён от Home Assistant и приложений, которые он контролирует.

## 14. Frigate и аппаратное ускорение

Frigate является целевым программным NVR и размещается отдельно от Home Assistant.

Целевые функции:

- RTSP/ONVIF;
- запись main stream;
- детекция по substream;
- OpenVINO;
- использование Intel N150/iGPU;
- события для Home Assistant.

Рабочий ID:

```text
500  Frigate
```

Окончательно выбрать VM или LXC после проверки удобства доступа к iGPU и фактической нагрузки.

Coral TPU на первом этапе не требуется.

Под видео на дополнительном SSD 512 ГБ предварительно рассматривается до **~250 ГБ**. Остальное пространство предполагается оставить под backup и другие задачи. Фактический объём определяется после измерения bitrate и retention.

## 15. Storage и резервное копирование

Резервирование должно существовать на нескольких уровнях:

- backup критичных VM/LXC средствами Proxmox;
- snapshot перед рискованными изменениями;
- отдельные backup конфигураций Home Assistant;
- backup Docker Compose и persistent data;
- backup сетевой конфигурации `network-gateway`;
- backup Gitea и других сервисов с пользовательскими данными;
- backup конфигурации Frigate.

Не считать snapshot полноценной заменой внешнему/отдельному backup.

Отдельный `backup-services` пока не обязателен; ID `410` зарезервирован на случай, если такой функциональный гость станет нужен.

Конкретное хранилище, retention и расписание backup остаются открытым вопросом.

## 16. Порядок запуска и зависимости

VMID/CTID не является startup order.

Предпочтительная логика зависимостей:

```text
Proxmox host
  ↓
network-gateway
  ↓
базовые инфраструктурные гости
  ├── automation-services
  ├── ai-services
  ├── monitoring
  └── app-services
  ↓
Home Assistant / AI Control / Frigate и прочие зависимые сервисы
```

Фактический порядок зависит от того, станет ли `network-gateway` default gateway для самого Proxmox и управляющих VM. Нельзя создавать циклическую зависимость, при которой запуск `network-gateway` требует сети, доступной только после запуска самого `network-gateway`.

Критические управляющие пути должны иметь fail-safe вариант восстановления.

## 17. Security и доступ

Принципы:

- отдельные Proxmox API tokens;
- минимально необходимые роли;
- отдельные SSH-ключи по классам систем;
- секреты не хранить в открытом Git;
- журналировать действия AI Control;
- опасные операции подтверждать;
- доступ к Proxmox management ограничивать доверенной сетью;
- не публиковать Web UI/API Proxmox напрямую в интернет без необходимости;
- сетевые изменения выполнять с rollback-механизмом.

Особенно защищать:

- `vmbr0`;
- management IP Proxmox;
- default gateway;
- storage;
- глобальный firewall;
- API tokens;
- SSH root.

## 18. Конфигурация как код

Целевая структура управляющего репозитория:

```text
home-infra/
├── proxmox/
│   ├── vm/
│   ├── lxc/
│   └── backup/
├── network/
│   ├── vlan/
│   ├── routing/
│   ├── nftables/
│   ├── dns/
│   ├── pbr/
│   └── vpn/
├── docker/
│   ├── automation-services/
│   ├── app-services/
│   ├── ai-services/
│   ├── monitoring/
│   └── network-gateway/
├── home-assistant/
├── ansible/
│   ├── inventory/
│   ├── roles/
│   └── playbooks/
└── docs/
```

Главный принцип — повторяемые операции оформлять как playbook/script/config, а не каждый раз генерировать непроверяемый набор shell-команд.

## 19. Открытые вопросы

1. Зафиксировать фактическую текущую конфигурацию Proxmox: версия, hostname, management IP, bridge, storage, swap, root filesystem и текущие VMID/CTID.
2. Определить точные CPU/RAM/storage лимиты всех целевых гостей.
3. Проверить ресурсы `network-gateway` под фактическую DNS/VPN/PBR-нагрузку.
4. Определить startup/shutdown order и задержки запуска.
5. Выбрать окончательный вариант Frigate: LXC или VM.
6. Протестировать iGPU/OpenVINO на 7–8 фактических камерах.
7. Подтвердить объём storage под Frigate и backup.
8. Определить постоянное хранилище и расписание Proxmox Backup.
9. Определить remote Docker deploy для всех разрешённых VM/LXC через SSH/Ansible.
10. Зафиксировать inventory Docker-hosts и allowlist сервисов.
11. Определить ресурсные лимиты `330 ai-services` и конкретные STT/TTS реализации.
12. Проверить финальные firewall-правила между MAIN/IOT/CAMERAS и management-сегментом.
13. Продумать аварийный доступ при отказе `network-gateway` или AI Control.
14. Определить, нужен ли отдельный Proxmox Backup Server в будущем.

## 20. Связанные материалы

- [200_section_index.md](200_section_index.md) — индекс раздела;
- [201_network_equipment_plan.md](201_network_equipment_plan.md) — общий паспорт сети;
- [203_network_segments_and_routing.md](203_network_segments_and_routing.md) — VLAN/firewall;
- [205_server_network_and_services.md](205_server_network_and_services.md) — распределение серверных сервисов;
- [207_network_migration_and_open_items.md](207_network_migration_and_open_items.md) — миграция и проверки;
- [208_network_gateway_vm.md](208_network_gateway_vm.md) — DNS/VPN/PBR VM;
- [209_ai_control_agent.md](209_ai_control_agent.md) — AI Control Agent;
- [210_security_and_cameras_plan.md](210_security_and_cameras_plan.md) — камеры и безопасность;
- [213_frigate_and_video_storage.md](213_frigate_and_video_storage.md) — Frigate, storage и AI.
