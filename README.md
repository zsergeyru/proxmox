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

Используется один основной monorepo `proxmox`. VM/LXC не обязаны клонировать весь репозиторий; повторяемое применение управляемых файлов выполняется Ansible на `311-dev-services` по SSH.

## Гости

| ID | Тип | Имя | Статус/назначение |
|---:|---|---|---|
| 100 | VM | `haos` | текущий production Home Assistant |
| 109 | VM | `network-gateway` | DNS, VPN, PBR, remote-access VPN |
| 201 | LXC | `ha-main` | возможная будущая миграция с 100 |
| 202 | LXC | `ha-test` | тестовый HA |
| 203 | LXC | `ha-flat2` | второй HA при необходимости |
| 211 | LXC | `automation-services` | MQTT, Zigbee2MQTT, ESPHome |
| 301 | VM | `ai-control` | целевой AI control plane |
| 311 | LXC | `dev-services` | Ansible, Semaphore, Git/CI |
| 320 | VM | `ai-control` | текущий bootstrap до перехода на 301 |
| 321 | LXC | `app-services` | Homarr и прикладные сервисы |
| 331 | LXC | `ai-services` | STT/TTS API |
| 401 | LXC | `monitoring` | мониторинг |
| 501 | VM/LXC | `frigate` | видеонаблюдение после теста |

Запланированный VMID не означает, что гостя нужно создавать заранее.

## Структура

```text
proxmox/
├── README.md
├── docs/
├── host/pve/
├── guests/
├── ansible/
├── scripts/
├── templates/
└── archive/
```

## Управление и deploy

```text
Proximo MCP
→ runtime lifecycle VM/LXC

deploy-guest на PVE
→ человек / deterministic PLAN/APPLY из guest manifests
→ ограниченный bootstrap для handoff к provisioning

Ansible Execution Environment на 311-dev-services
→ повторяемая настройка гостевых ОС по SSH

прямой SSH
→ bootstrap, диагностика и аварийные действия

Semaphore
→ необязательный web-интерфейс к Ansible
```

Для управляемой Debian-инфраструктуры используется единый management user `root`. Пароль root заблокирован; SSH допускается только по public key. Разные потребители используют разные SSH identities, а не разные Linux-пользователи.

## Сеть

До переезда Keenetic остаётся edge-router, а `109-network-gateway` работает в общей LAN.

После переезда OpenWrt отвечает за WAN, VLAN, DHCP и базовый L3/firewall. `109` отвечает за SmartDNS, VPN/PBR, remote-access VPN и сопутствующие сервисы.

Guest network описывается централизованно:

```text
guests/defaults.yaml
→ active subnet + gateway + bridge

VMID
→ management IP

guest.yaml
→ optional IP override только для исключения
```

В каждый момент времени у гостя один management IP и один default gateway.

Источник истины по сети: [`docs/40-network.md`](docs/40-network.md).

## Источники истины

При расхождении использовать такой приоритет:

1. `guests/defaults.yaml` + существующий `guests/*/guest.yaml` — deploy desired state VM/LXC.
2. [`docs/11-vmid-plan.md`](docs/11-vmid-plan.md) — план VMID/CTID и описание VMID-addressing rule.
3. ADR в `guests/<guest>/decisions/` — решения конкретного гостя.
4. профильный документ в `docs/`.
5. README конкретного гостя — эксплуатационные особенности.
6. [`docs/10-architecture.md`](docs/10-architecture.md) — сводка.

Наблюдаемое состояние PVE хранится в `host/pve/`, временный drift конкретного гостя — в его `STATUS.md`.

## Главные документы

- [`docs/README.md`](docs/README.md) — оглавление.
- [`docs/10-architecture.md`](docs/10-architecture.md) — сводная архитектура.
- [`docs/11-vmid-plan.md`](docs/11-vmid-plan.md) — VMID/CTID и addressing rule.
- [`docs/30-guest-manifest.md`](docs/30-guest-manifest.md) — guest/defaults contract.
- [`docs/33-guest-bootstrap-and-provisioning.md`](docs/33-guest-bootstrap-and-provisioning.md) — limited deployer bootstrap, 311 и Ansible provisioning.
- [`docs/40-network.md`](docs/40-network.md) — IPv4/VLAN/VPN.
- [`docs/41-dns.md`](docs/41-dns.md) — SmartDNS, `home.arpa`, mDNS.
- [`docs/50-ai-control.md`](docs/50-ai-control.md) — AI-управление.
- [`docs/22-storage-and-backup.md`](docs/22-storage-and-backup.md) — backup/restore.
- [`templates/debian13/README.md`](templates/debian13/README.md) — Debian 13 template.

## Общие правила

- Git — source of truth для повторяемой конфигурации;
- на гость разворачивается только его собственное содержимое `rootfs/`;
- persistent data и secrets не являются `rootfs/`;
- deployer может выполнять только ограниченный bootstrap, необходимый для handoff;
- Ansible — штатный повторяемый deploy внутри Linux-гостей;
- SSH сохраняется как базовый административный и аварийный канал;
- root password authentication отключён, доступ выдаётся независимыми SSH-ключами;
- secrets, tokens, passwords и private keys в Git не добавляются.
