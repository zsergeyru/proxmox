# Proxmox host: `pve`

Этот каталог относится только к конфигурации самого гипервизора.

Прикладные сервисы на Proxmox host не устанавливать. Home Assistant, DNS/VPN, AI, monitoring, Frigate и обычные Docker-приложения должны жить в VM/LXC.

## Фактическое состояние

Снимок состояния снят с работающего сервера 2026-09-12.

### Система

```text
hostname: pve
Proxmox VE: 9.2.18
proxmox-ve package: 9.2.0
Debian: 13 (trixie)
kernel: 7.0.14-16-pve
architecture: x86-64
```

### Аппаратная платформа

```text
model: Guangdong BuerTech BREN9E
CPU: Intel N150
cores/threads: 4 / 4
RAM: 15 GiB usable
firmware: AMI 5.27, 2025-04-27
```

На момент снимка температура CPU была около `58 °C`, NVMe — около `33 °C`.

### Текущие реальные гости

На хосте фактически существуют две VM:

```text
100  HAOS      running
320  ai-agent  running
```

LXC-контейнеров на момент снимка нет.

VM `100` описывается в [`../../guests/100-haos/`](../../guests/100-haos/) и является текущей production-системой Home Assistant. Исторический VMID сохраняется; отдельная миграция на `201` потребуется только при появлении практической причины.

VM `320` соответствует bootstrap-каталогу [`../../guests/320-ai-control/`](../../guests/320-ai-control/). Имя объекта в Proxmox сейчас `ai-agent`, хотя архитектурное имя каталога — `ai-control`.

## Структура каталога хоста

- [`network/`](network/) — фактическая сеть хоста и bridge;
- [`storage/`](storage/) — storage, LVM и mount points;
- [`backup/`](backup/) — текущее backup-хранилище и открытые вопросы;
- общая целевая архитектура сети находится в [`../../docs/40-network.md`](../../docs/40-network.md).

## Принцип

Здесь фиксируется наблюдаемое состояние самого PVE-хоста. Целевые параметры VM/LXC принадлежат их каталогам `guests/`, а прикладные конфигурации внутри гостей — их `rootfs/`.
