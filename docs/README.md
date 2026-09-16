# Документация Proxmox

`docs/` содержит архитектуру, policy, specifications, reference и runbooks проекта. Этот README — навигатор: он помогает найти канонический документ, но сам не повторяет его технические значения.

## Быстрый маршрут

| Задача | Куда идти |
|---|---|
| Понять общую архитектуру | [`10-architecture.md`](10-architecture.md) |
| Найти VMID/CTID и addressing plan | [`11-vmid-plan.md`](11-vmid-plan.md) |
| Поднять/инициализировать PVE host | [`20-pve-initialization.md`](20-pve-initialization.md) |
| Понять host-side каталоги/state/credentials | [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) |
| Storage, backup, restore | [`22-storage-and-backup.md`](22-storage-and-backup.md) |
| SSH identities и общая security policy | [`23-security.md`](23-security.md) |
| Versions/reproducibility/bootstrap contract | [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) |
| Точные PVE roles/privileges/ACL | [`25-pve-access-control.md`](25-pve-access-control.md) |
| Простое объяснение deployer ↔ AI flow | [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md) |
| Формат `guest.yaml` и merge semantics | [`30-guest-manifest.md`](30-guest-manifest.md) |
| Initial SSH, bootstrap и Ansible handoff | [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) |
| Размещение кода/config/data внутри Linux | [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md) |
| IPv4/VLAN/VPN/PBR | [`40-network.md`](40-network.md) |
| SmartDNS/DoH/DoT/home.arpa | [`41-dns.md`](41-dns.md) |
| IPv6 | [`42-ipv6.md`](42-ipv6.md) |
| AI control plane | [`50-ai-control.md`](50-ai-control.md) |
| Bootstrap `301-ai-control` | [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) |

Template `9000` документируется отдельно в [`../templates/debian13/README.md`](../templates/debian13/README.md) и [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## Карта документов

| Документ | Тип | Что он владеет |
|---|---|---|
| [`10-architecture.md`](10-architecture.md) | Overview | Сводная картина платформы; не должен дублировать детальные contracts |
| [`11-vmid-plan.md`](11-vmid-plan.md) | Plan / Reference | VMID/CTID plan и addressing convention |
| [`20-pve-initialization.md`](20-pve-initialization.md) | Runbook | Каноническая процедура инициализации PVE |
| [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) | Reference | Host-side filesystem/state/credential layout |
| [`22-storage-and-backup.md`](22-storage-and-backup.md) | Policy / Runbook | Storage, backup, retention, restore |
| [`23-security.md`](23-security.md) | Policy | Общая security и SSH identity model |
| [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) | Policy / Reference | Versioning, reproducibility и project contract versions |
| [`25-pve-access-control.md`](25-pve-access-control.md) | Policy / Reference | Канонические PVE identities, roles, privileges и ACL |
| [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md) | Overview | Человеческое объяснение access/deploy flow; не владеет точными ACL |
| [`30-guest-manifest.md`](30-guest-manifest.md) | Specification | Канонический manifest/defaults/effective-state contract |
| [`31-bootstrap.md`](31-bootstrap.md) | Overview / Status | Сводка bootstrap-схемы; детали принадлежат `20`, `21`, `24` и template policy |
| [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md) | Policy | Docker внутри unprivileged LXC |
| [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) | Policy / Design | Management readiness, bootstrap boundary и Ansible handoff |
| [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md) | Policy / Reference | `/opt`, `/etc`, `/var/lib`, `/srv`, logs/cache/runtime |
| [`40-network.md`](40-network.md) | Policy / Reference | IPv4, VLAN, routing, VPN/PBR boundary |
| [`41-dns.md`](41-dns.md) | Policy / Reference | DNS architecture and policy |
| [`42-ipv6.md`](42-ipv6.md) | Policy / Design | IPv6/dual-stack model |
| [`50-ai-control.md`](50-ai-control.md) | Architecture | Target AI control plane |
| [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) | Design / Runbook | Bootstrap requirements for `301-ai-control` |
| [`52-local-ai.md`](52-local-ai.md) | Design note | Local AI direction |
| [`60-apartment-infrastructure.md`](60-apartment-infrastructure.md) | Boundary / Overview | Граница server infrastructure ↔ apartment project |

## Источники истины

Числовой префикс — только навигация. Источник истины определяется предметной областью.

Основные правила:

- machine-readable desired state гостя — `guests/defaults.yaml` + `guests/<guest>/guest.yaml`;
- schema/merge semantics manifests — `30-guest-manifest.md` + `schemas/`;
- точные PVE permissions — `25-pve-access-control.md`;
- общая credential/SSH policy — `23-security.md`;
- template `9000` — `templates/debian13/build-policy.md`;
- фактическое состояние работающего host — `host/pve/`;
- устойчивое решение конкретного гостя — его ADR;
- временное observed состояние/drift — `STATUS.md`.

Overview и README должны ссылаться на эти источники, а не копировать точные версии, privilege sets, IP, template parameters или другие быстро меняющиеся значения.

## Типы документов

Для крупных документов рекомендуется в начале указывать:

```text
Type: Overview | Architecture | Policy | Reference | Runbook | Specification | Design | Plan
Status: Active | Draft | Historical
Source of truth: Yes | No — для какой именно предметной области
```

`Source of truth: Yes` не означает, что документ главнее всех остальных. Он владеет только явно указанной областью.

## Нумерация

```text
10–19  architecture / inventory plan
20–29  PVE host / storage / security / access
30–39  guest deploy / provisioning / Linux policy
40–49  network
50–59  AI control plane
60–69  external integration boundaries
```

Новые документы получают номер внутри подходящего блока. Существующие не перенумеровываются ради плотной последовательности.

Отдельный `90-roadmap.md` не ведётся. Временный backlog не является архитектурным source of truth; текущий guest status при необходимости хранится в `STATUS.md`, а устойчивые решения переходят в policy/spec/ADR.
