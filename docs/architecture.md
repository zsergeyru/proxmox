# Proxmox — сводная архитектура платформы

## 1. Назначение документа

Этот файл описывает общую архитектуру домашней Proxmox-платформы и границы подсистем. Он не дублирует все параметры VM/LXC и файловую структуру каждого гостя.

Источники истины для деталей:

- [`vmid-plan.md`](vmid-plan.md) — актуальные VMID/CTID и правила адресации;
- [`../guests/README.md`](../guests/README.md) — формат `guest.yaml`, `rootfs/` и правила каталогов гостей;
- README и ADR конкретного гостя — решения и эксплуатация конкретной VM/LXC;
- [`network.md`](network.md) — сеть Proxmox и роль `network-gateway`;
- [`ai-control.md`](ai-control.md) — AI-контур управления;
- [`storage-and-backup.md`](storage-and-backup.md) — резервное копирование и storage.

При расхождении этого документа с профильным документом приоритет имеет профильный документ.

## 2. Аппаратная платформа

```text
CPU: Intel N150
RAM: 16 ГБ
System SSD: M.2 512 ГБ
Hypervisor: Proxmox VE
```

На хосте Proxmox не размещаются обычные прикладные сервисы. Хост отвечает за виртуализацию, storage, bridge/VLAN и необходимые системные функции Proxmox.

## 3. Принцип разделения

Отдельная VM используется, когда нужна сильная изоляция, собственный сетевой стек или потенциально опасный управляющий контур. LXC используется для лёгких функциональных групп сервисов. Docker Compose допускается внутри VM/LXC, если гость является логическим контейнером для группы приложений.

Целевая карта новых гостей:

```text
101  VM       network-gateway
201  LXC      ha-main
202  LXC      ha-test
203  LXC      ha-flat2
211  LXC      automation-services
301  VM       ai-control
311  LXC      dev-services
321  LXC      app-services
331  LXC      ai-services
401  LXC      monitoring
501  VM/LXC   frigate
```

`320-ai-control` остаётся текущим bootstrap-узлом до запуска и проверки `301-ai-control`. Existing legacy VM не перенумеровываются только ради унификации схемы.

## 4. Роли гостей

### 101 — network-gateway

Отдельная Debian VM для пользовательской маршрутизации:

- Linux routing и policy routing;
- `nftables`;
- VPN-клиенты;
- DNS-сервисы;
- health-check/watchdog.

Маршрутизация, VPN и firewall принадлежат ОС VM. AdGuard Home/SmartDNS могут работать в Docker, но Docker networking не должен подменять сетевую роль VM.

### 201/202/203 — Home Assistant

- `201-ha-main` — основной Home Assistant новой квартиры;
- `202-ha-test` — тестирование обновлений и интеграций;
- `203-ha-flat2` — резерв под вторую квартиру при практической необходимости.

Существующий перенесённый HAOS считается legacy и обслуживается отдельно от этой целевой схемы.

### 211 — automation-services

Общая инфраструктура домашней автоматики:

- Mosquitto/MQTT;
- Zigbee2MQTT;
- ESPHome Dashboard.

### 301 — ai-control

Целевой управляющий контур с Hermes/другими агентами. Базовые каналы управления:

```text
Proxmox MCP → жизненный цикл VM/LXC
SSH         → ОС, файлы, systemd, Docker/Compose и приложения внутри гостей
```

Отдельный универсальный `deploy-mcp` не является частью целевой архитектуры.

### 311 — dev-services

DevOps-сервисы отделены от обычных приложений:

- Gitea/Gogs;
- Jenkins;
- другие инструменты разработки и CI при необходимости.

### 321 — app-services

Некритичные приложения общего назначения:

- Homarr;
- password server;
- torserver;
- другие небольшие сервисы того же класса.

### 331 — ai-services

Общие AI API, не привязанные к конкретному агенту:

- Whisper/faster-whisper STT;
- TTS (Kokoro/Piper или выбранный движок).

### 401 — monitoring

Минимально Uptime Kuma; далее по необходимости Prometheus/Grafana/InfluxDB/syslog и другие средства наблюдения.

### 501 — Frigate

Отдельный гость для видеозаписи, RTSP/ONVIF, детекции и OpenVINO/iGPU. Выбор VM или LXC окончательно определяется практическим тестом доступа к iGPU и нагрузкой камер.

## 5. Сеть Proxmox

Физический сервер использует один uplink и VLAN-aware bridge. Proxmox не должен становиться неявным маршрутизатором между сегментами.

Целевые логические сегменты включают MAIN, IOT и CAMERAS. Конкретная адресация и VLAN описываются отдельно; пользовательская маршрутизация, DNS, VPN и PBR относятся к `101-network-gateway`.

## 6. Конфигурация как код

Репозиторий построен вокруг гостей:

```text
guests/<VMID>-<name>/
├── README.md
├── guest.yaml
├── decisions/        # при необходимости
├── STATUS.md         # при необходимости
└── rootfs/
```

`guest.yaml` описывает сам объект VM/LXC в Proxmox. `rootfs/` повторяет абсолютные пути внутри гостевой ОС. Отдельных универсальных `files/` и `services/` нет.

Пример:

```text
guests/101-network-gateway/rootfs/etc/nftables.conf
→ /etc/nftables.conf
```

Файлы конкретного приложения хранятся в каталоге того гостя, где приложение реально работает. `ai-control` не содержит копии конфигураций других VM/LXC.

## 7. AI-управление

Агент рассматривается как администратор инфраструктуры с достаточными правами для поставленных задач.

Типовой цикл:

```text
создать/изменить VM через Proxmox MCP
→ дождаться загрузки
→ подключиться по SSH
→ подготовить ОС
→ синхронизировать rootfs
→ запустить/обновить приложение
→ проверить health/status/logs
→ зафиксировать повторяемую конфигурацию в Git
```

Подробное принятое решение находится в ADR текущего bootstrap-узла: [`../guests/320-ai-control/decisions/001-simple-management-model.md`](../guests/320-ai-control/decisions/001-simple-management-model.md).

## 8. Безопасность и восстановление

Основные принципы:

- секреты и приватные ключи не хранятся в Git;
- используются отдельные Proxmox API tokens и SSH-ключи;
- опасные изменения должны иметь rollback-план;
- перед рискованными изменениями используются snapshots/backup;
- persistent data и Docker volumes резервируются отдельно от конфигурации;
- после изменений обязательно проверяются health/status/logs;
- действия управляющего агента должны журналироваться.

Особенно критичны management-сеть, bridge, gateway, firewall, storage и операции удаления VM/LXC.

## 9. Порядок запуска

VMID не является startup order. Порядок запуска задаётся отдельно и должен учитывать зависимости без циклов.

Общая логика:

```text
Proxmox host
  ↓
network-gateway (если от него зависит пользовательская сеть)
  ↓
базовые инфраструктурные сервисы
  ├── automation-services
  ├── ai-services
  ├── monitoring
  └── dev/app-services
  ↓
Home Assistant / AI Control / Frigate и зависимые приложения
```

При проектировании `network-gateway` обязательно сохраняется аварийный путь доступа к Proxmox, не зависящий от успешной загрузки этой VM.

## 10. Граница с проектом квартиры

Общий проект физической сети, камер, оборудования и квартиры остаётся в `zsergeyru/appart-rennovation`. В этот репозиторий переносятся только серверные и эксплуатационные части, которые непосредственно относятся к Proxmox и его гостям.

См. [`apartment-infrastructure.md`](apartment-infrastructure.md).
