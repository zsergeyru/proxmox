# Документация Proxmox

Документы в этом каталоге имеют числовые префиксы. Номер задаёт тематический блок и рекомендуемый порядок чтения, а не жёсткую последовательность создания файлов.

## Порядок чтения

### 10–19 — архитектура и карта инфраструктуры

- [`10-architecture.md`](10-architecture.md) — сводная архитектура платформы.
- [`11-vmid-plan.md`](11-vmid-plan.md) — VMID/CTID и management IP.

### 20–29 — Proxmox host, хранение и безопасность

- [`20-pve-initialization.md`](20-pve-initialization.md) — инициализация нового Proxmox VE host.
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — файловая структура PVE bootstrap/deployer.
- [`22-storage-and-backup.md`](22-storage-and-backup.md) — storage, backup, retention и restore.
- [`23-security.md`](23-security.md) — **каноническая общая security policy**: root key-only management, независимые PVE/AI/Ansible SSH identities и private-key lifecycle.
- [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) — versioning/reproducibility policy и project contract versions.
- [`25-pve-access-control.md`](25-pve-access-control.md) — **каноническая техническая policy** PVE identities, roles, privileges, ACL и границы `managed`.
- [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md) — **короткий обзор для человека**: `root`, `pvedeploy`, host deployer, AI agent, Git и два независимых management flow.

Точные Proxmox privileges и ACL не дублируются в `26`: при расхождении всегда действует `25-pve-access-control.md`.

Диапазон `27–29` зарезервирован для новых документов уровня PVE.

### 30–39 — развёртывание и устройство Linux-гостей

- [`30-guest-manifest.md`](30-guest-manifest.md) — канонический формат `guest.yaml`, root management contract и PVE-side deploy.
- [`31-bootstrap.md`](31-bootstrap.md) — текущее состояние Stage 0/Stage 1 и bootstrap safety.
- [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md) — Docker внутри unprivileged LXC, security boundary и backup/restore gate.
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — initial management readiness, deployer bootstrap ↔ Ansible provisioning и bootstrap capabilities.
- [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md) — **каноническая policy** размещения application/config/persistent data/logs/cache/runtime внутри Linux VM/LXC.

Диапазон `35–39` зарезервирован для общих deploy/guest policy.

### 40–49 — сеть

- [`40-network.md`](40-network.md) — IPv4, VLAN, VPN/PBR и граница OpenWrt ↔ VM 109.
- [`41-dns.md`](41-dns.md) — SmartDNS, `home.arpa`, DoH/DoT и DNS policy.
- [`42-ipv6.md`](42-ipv6.md) — IPv6 и dual-stack модель.

### 50–59 — AI control plane

- [`50-ai-control.md`](50-ai-control.md) — целевая архитектура AI-управления, PVE ACL и direct root SSH.
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) — bootstrap AI control plane и SSH identities.
- [`52-local-ai.md`](52-local-ai.md) — локальный AI.

### 60–69 — интеграция с квартирой

- [`60-apartment-infrastructure.md`](60-apartment-infrastructure.md) — граница между серверной инфраструктурой и проектом квартиры.

## Правило нумерации

Новый документ получает номер внутри соответствующего тематического блока. Существующие файлы не перенумеровываются ради плотной последовательности.

Временный backlog/roadmap не является архитектурной документацией и в `docs/` отдельным блоком не ведётся. Текущие состояния конкретных гостей фиксируются в `guest.yaml` и при необходимости `STATUS.md`; устойчивые решения переносятся в профильные документы или ADR.

## Источники истины

Числовой префикс задаёт навигацию, но не приоритет. При конфликте действует приоритет корневого [`../README.md`](../README.md), машинно-читаемых guest defaults/manifests, профильных policy и актуальных ADR.
