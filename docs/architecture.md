# Proxmox — сводная архитектура платформы

Этот документ даёт только общую картину. Детали не дублируются здесь и берутся из профильных источников истины:

- [`vmid-plan.md`](vmid-plan.md) — VMID/CTID и management IP;
- [`network.md`](network.md) — сеть, VLAN, VPN и граница OpenWrt ↔ 101;
- [`../guests/README.md`](../guests/README.md) — `guest.yaml`, `rootfs` и deploy;
- [`ai-control.md`](ai-control.md) — AI-контур;
- [`storage-and-backup.md`](storage-and-backup.md) — backup и restore;
- README/ADR конкретного гостя — локальные особенности.

## Аппаратная платформа

```text
CPU: Intel N150
RAM: 16 GiB
System SSD: M.2 512 GiB
Hypervisor: Proxmox VE
```

PVE остаётся максимально чистым гипервизором: обычные прикладные сервисы живут в VM/LXC.

## Разделение VM/LXC

Отдельная VM используется для сильной изоляции, отдельного сетевого стека или управляющего контура. LXC используется для лёгких функциональных групп. Docker Compose допускается внутри VM/LXC, если гость является логическим контейнером группы приложений.

Ключевые роли:

```text
100  VM       HAOS production
101  VM       network-gateway
201  LXC      optional HA migration target
211  LXC      automation-services
301  VM       target ai-control
311  LXC      dev-services
320  VM       bootstrap ai-control
321  LXC      app-services
331  LXC      ai-services
401  LXC      monitoring
501  VM/LXC   frigate
```

Гости `202`, `203`, `401` и другие плановые роли создаются только при реальной необходимости.

## Home Assistant

VM `100 HAOS` — текущая production-система и сохраняет исторический VMID. `201-ha-main` — только возможная будущая цель миграции; перенос с 100 не выполняется ради одной нумерации.

MQTT/Zigbee2MQTT/ESPHome выносятся в `211-automation-services`, если это практически оправдано.

## Network gateway

`101-network-gateway` предоставляет:

- SmartDNS;
- DoH/DoT;
- исходящие VPN;
- PBR через nftables/fwmark/ip rule;
- remote-access VPN;
- mDNS reflection при необходимости.

После переезда физический OpenWrt отвечает за WAN, локальные VLAN, DHCP и базовый inter-VLAN firewall/L3. Поэтому отказ 101 или PVE не должен отключать обычный интернет и базовую маршрутизацию квартиры.

## AI и DevOps

Разделение ответственности:

```text
Proxmox MCP
→ lifecycle VM/LXC, snapshots, backups

Ansible на 311-dev-services
→ повторяемая конфигурация Linux-гостей по SSH

прямой SSH
→ bootstrap, диагностика и аварийные действия

Semaphore
→ необязательный ручной UI к Ansible
```

`301-ai-control` содержит AI-агентов, MCP и интерфейсы AI. Ansible/Semaphore/Git/CI относятся к `311-dev-services`.

`320-ai-control` — временный bootstrap до проверки 301.

## Конфигурация как код

Типовой гость:

```text
guests/<VMID>-<name>/
├── README.md
├── guest.yaml
├── decisions/
├── STATUS.md
└── rootfs/
```

`guest.yaml` описывает объект Proxmox. `rootfs/` содержит только управляемые проектом файлы по реальным абсолютным путям гостевой ОС.

Для Debian-инфраструктуры единый административный SSH-пользователь — `ops`.

## Deploy

Целевой цикл изменения:

```text
изменить конфигурацию в Git
→ при необходимости изменить VM/LXC через Proxmox MCP
→ выполнить bootstrap по SSH
→ применить конфигурацию Ansible из 311
→ проверить health/status/logs
→ зафиксировать результат
```

Глобальный `rsync --delete rootfs/ /` запрещён. Удаляющая синхронизация допустима только внутри каталогов, полностью принадлежащих проекту.

## Безопасность и восстановление

- секреты/private keys не хранятся в Git;
- используются отдельные API tokens/SSH keys;
- перед рискованными изменениями применяются snapshot/backup по ситуации;
- persistent data резервируется отдельно от конфигурации;
- критичные backup регулярно проверяются реальным restore-test;
- действия автоматизации журналируются;
- после изменений проверяются health/status/logs.

Подробная backup-политика: [`storage-and-backup.md`](storage-and-backup.md).

## Startup order

VMID не задаёт порядок запуска. Startup order определяется по зависимостям.

Важно: базовый доступ к PVE и базовая L3-сеть квартиры не должны зависеть от успешного запуска `101-network-gateway`, `311-dev-services` или `301-ai-control`.

## Граница с проектом квартиры

Физическая сеть, камеры, оборудование и общие решения квартиры остаются в `zsergeyru/appart-rennovation`. В этот репозиторий попадают только серверные и эксплуатационные части, относящиеся к Proxmox и его гостям.

См. [`apartment-infrastructure.md`](apartment-infrastructure.md).
