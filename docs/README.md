# Документация Proxmox

`docs/` содержит проектную документацию. Файлы по-прежнему нумеруются **по предметной области**, а не раскладываются по каталогам типов: это сохраняет короткие ссылки и понятную тематическую последовательность.

При этом каждый крупный документ имеет **ровно один основной тип**:

```text
Overview
Policy
Specification
Reference
Runbook
```

Смешанные типы вроде `Policy / Reference` или `Design / Runbook` больше не используются. Если в одном файле накопились разные виды информации, её нужно разнести по отдельным документам и связать ссылками.

## Типы документов

### Overview

Объясняет систему человеку: что существует, как части связаны и куда идти дальше.

Overview:

- не владеет точными privilege sets, версиями, IP и machine-readable contracts;
- не содержит длинных пошаговых процедур;
- должен быть коротким и ссылаться на профильные источники истины.

### Policy

Фиксирует принятые правила, ограничения и причины решений.

Policy отвечает на вопросы:

```text
что разрешено / запрещено
какие гарантии требуются
какие safety boundaries приняты
почему проект действует именно так
```

Policy не должна превращаться в пошаговую инструкцию эксплуатации.

### Specification

Фиксирует **точный desired contract** системы или интерфейса.

Здесь допустимы:

- schema;
- exact fields/semantics;
- роли и privilege sets;
- network topology/contracts;
- machine-visible parameters;
- обязательные invariants.

Specification отвечает на вопрос **«каким должно быть состояние»**.

### Reference

Справочник для поиска фактов и структуры.

Примеры:

- filesystem paths;
- ownership/layout;
- перечень runtime/state locations;
- observed inventory, если документ явно так помечен.

Reference не должна повторять policy и не должна учить оператора последовательности действий.

### Runbook

Пошаговая эксплуатационная процедура.

Runbook отвечает на вопрос **«что делать оператору»** и может содержать команды, последовательность проверок, STOP conditions и acceptance checklist.

Runbook должен ссылаться на Policy/Specification/Reference вместо копирования их точных contracts.

## Быстрый маршрут

| Задача | Куда идти |
|---|---|
| Понять общую архитектуру | [`10-architecture.md`](10-architecture.md) |
| Найти VMID/CTID и addressing contract | [`11-vmid-plan.md`](11-vmid-plan.md) |
| Инициализировать или повторно применить PVE configuration | [`20-pve-initialization.md`](20-pve-initialization.md) |
| Найти host-side paths/state/credentials | [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) |
| Понять backup/retention/RPO/RTO policy | [`22-storage-and-backup.md`](22-storage-and-backup.md) |
| Выполнить restore/disaster recovery | [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md) |
| SSH identities и общая security policy | [`23-security.md`](23-security.md) |
| Versioning/reproducibility rules | [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) |
| Точные PVE roles/privileges/ACL | [`25-pve-access-control.md`](25-pve-access-control.md) |
| Простое объяснение deployer ↔ AI flow | [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md) |
| Формат `guest.yaml` и merge semantics | [`30-guest-manifest.md`](30-guest-manifest.md) |
| Docker inside LXC rules | [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md) |
| Initial SSH/bootstrap/provisioning contract | [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) |
| Размещение code/config/data внутри Linux | [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md) |
| IPv4/VLAN/VPN/PBR contract | [`40-network.md`](40-network.md) |
| DNS contract | [`41-dns.md`](41-dns.md) |
| IPv6 policy | [`42-ipv6.md`](42-ipv6.md) |
| AI control plane contract | [`50-ai-control.md`](50-ai-control.md) |
| Ввести `301-ai-control` в работу | [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) |
| Понять направление local AI | [`52-local-ai.md`](52-local-ai.md) |
| Граница server repo ↔ apartment repo | [`60-apartment-infrastructure.md`](60-apartment-infrastructure.md) |

Template `9000` документируется отдельно:

- [`../templates/debian13/README.md`](../templates/debian13/README.md) — Overview/passport;
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — фактически Specification полного template contract.

## Карта документов по типам

### Overview

| Документ | Область |
|---|---|
| [`10-architecture.md`](10-architecture.md) | общая картина платформы |
| [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md) | человеческое объяснение Git/deployer/AI flows |
| [`52-local-ai.md`](52-local-ai.md) | направление и роль local AI |

### Policy

| Документ | Что фиксирует |
|---|---|
| [`22-storage-and-backup.md`](22-storage-and-backup.md) | backup classes, retention, RPO/RTO, restore requirements |
| [`23-security.md`](23-security.md) | общая security и SSH credential model |
| [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) | versioning/reproducibility/source revision rules |
| [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md) | допустимые границы Docker inside unprivileged LXC |
| [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md) | правила размещения application/config/persistent data/logs/cache/runtime |
| [`42-ipv6.md`](42-ipv6.md) | принятая IPv6/dual-stack policy |
| [`60-apartment-infrastructure.md`](60-apartment-infrastructure.md) | граница ответственности server infrastructure и apartment project |

### Specification

| Документ | Что задаёт точно |
|---|---|
| [`11-vmid-plan.md`](11-vmid-plan.md) | VMID/CTID и addressing contract |
| [`25-pve-access-control.md`](25-pve-access-control.md) | PVE identities, roles, privileges, ACL, `managed` boundary |
| [`30-guest-manifest.md`](30-guest-manifest.md) | manifest/defaults/effective-state schema и semantics |
| [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) | management readiness/bootstrap/provisioning contract |
| [`40-network.md`](40-network.md) | IPv4/VLAN/routing/VPN/PBR architecture contract |
| [`41-dns.md`](41-dns.md) | DNS/home.arpa/SmartDNS/mDNS contract |
| [`50-ai-control.md`](50-ai-control.md) | target AI control plane contract |

### Reference

| Документ | Справочная область |
|---|---|
| [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) | host-side filesystem/state/credentials layout |

Observed state PVE также является Reference, но живёт отдельно в [`../host/pve/`](../host/pve/) и всегда датируется/описывается как фактическое состояние, а не desired state.

### Runbook

| Документ | Процедура |
|---|---|
| [`20-pve-initialization.md`](20-pve-initialization.md) | first run / rerun / resume PVE bootstrap/configuration |
| [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md) | restore-test и восстановление после потери host |
| [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) | создание, ввод и acceptance test `301-ai-control` |

## Что не является отдельным типом большой документации

### README

README — entry point или локальный индекс каталога. Он не должен становиться ещё одним source of truth при наличии профильной Specification/Policy.

### ADR / decision record

ADR фиксирует устойчивое решение **конкретного компонента/гостя** и причины выбора. ADR живёт рядом с объектом, например:

```text
guests/<guest>/decisions/
```

Он не заменяет общепроектную Policy или Specification.

### STATUS.md

`STATUS.md` — временное observed/drift состояние конкретного объекта. Это не архитектурная документация и не permanent backlog.

### План/backlog

Отдельный `90-roadmap.md` не ведётся. Незавершённая работа не должна смешиваться с действующими contracts. Реализованное устойчивое решение переносится в Policy/Specification/ADR; observed состояние — в `STATUS.md`.

## Source-of-truth rules

Числовой префикс — только навигация. Источник истины определяется областью.

Основные владельцы:

```text
guest desired state
→ guests/defaults.yaml + guests/<guest>/guest.yaml

manifest semantics
→ 30-guest-manifest.md + schemas/

PVE permissions
→ 25-pve-access-control.md

SSH/security
→ 23-security.md

PVE filesystem/runtime paths
→ 21-pve-filesystem-layout.md

backup guarantees
→ 22-storage-and-backup.md

operator recovery procedure
→ 27-backup-and-disaster-recovery-runbook.md

template 9000 exact contract
→ templates/debian13/build-policy.md

actual running PVE state
→ host/pve/
```

Overview/README/Runbook должны ссылаться на эти owners, а не копировать быстро меняющиеся contract values.

## Metadata крупных документов

При создании или существенной переработке документа использовать единый заголовок:

```text
Type: Overview | Policy | Specification | Reference | Runbook
Status: Active | Draft | Historical
Source of truth: Yes | No — и для какой области
```

У документа должен быть **один** `Type`.

Если не удаётся выбрать один тип, это сигнал, что файл нужно разделить.

## Нумерация

Нумерация остаётся тематической:

```text
10–19  architecture / inventory contracts
20–29  PVE host / storage / security / access / recovery
30–39  guest deploy / provisioning / Linux policy
40–49  network
50–59  AI control plane
60–69  external integration boundaries
```

Новый документ получает номер внутри соответствующего тематического блока. Файлы не перенумеровываются только ради плотной последовательности.