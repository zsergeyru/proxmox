# Proxmox — сводная архитектура платформы

**Type:** Overview  
**Status:** Active  
**Source of truth:** No — это сводная картина; точные contracts находятся в профильных документах.

Этот документ отвечает только на вопрос **«из каких частей состоит система и как они связаны»**. Он намеренно не дублирует VMID, IP, privilege sets, template version, filesystem paths и другие быстро меняющиеся значения.

## 1. Общая модель

```text
Physical PVE host
      │
      ├─ VM/LXC infrastructure
      │    ├─ Home Assistant / automation
      │    ├─ network services
      │    ├─ AI control plane
      │    ├─ DevOps / Ansible
      │    ├─ application services
      │    └─ monitoring / video
      │
      ├─ host-side deploy tooling
      └─ backup / recovery
```

PVE остаётся максимально чистым гипервизором. Обычные application services размещаются внутри VM/LXC, а не непосредственно на host.

Фактическое состояние host хранится в [`../host/pve/`](../host/pve/).

## 2. Desired state и фактическое состояние

Проект разделяет:

```text
Git desired state
→ что должно быть

PVE / guest runtime
→ что реально работает сейчас
```

Guest desired state задаётся через:

```text
guests/defaults.yaml
+
guests/<guest>/guest.yaml
+
schemas/
```

Точный contract: [`30-guest-manifest.md`](30-guest-manifest.md).

Observed host state хранится в `host/pve/`, а временный drift конкретного guest — в `STATUS.md`.

## 3. VM и LXC

VM используется там, где нужна более сильная изоляция, отдельный kernel/network stack или специальный control plane.

LXC используется для лёгких доверенных infrastructure/application workloads, когда это не нарушает security boundary.

Docker внутри LXC допускается только по отдельной project policy: [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md).

VMID/CTID и addressing convention находятся в [`11-vmid-plan.md`](11-vmid-plan.md).

## 4. Управление Proxmox

Есть два независимых PVE management flow.

### Человек / host-side tooling

```text
operator
→ deploy-guest
→ deployer@pve!host-deploy
→ Proxmox API
```

Этот путь нужен для deterministic PLAN/APPLY из Git desired state и специальных host-side операций.

### AI Control

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ managed guest lifecycle
```

AI использует отдельную PVE identity и ограниченную write-zone.

Человеческое объяснение схемы: [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md).  
Точный access contract: [`25-pve-access-control.md`](25-pve-access-control.md).

## 5. Управление Linux внутри guests

PVE lifecycle и configuration внутри guest — разные уровни.

```text
PVE lifecycle/config
→ deploy-guest / Proximo

initial management readiness
→ root SSH key-only

repeatable OS/application configuration
→ Ansible на dev-services

one-off / diagnostics
→ direct SSH соответствующей identity
```

Initial access/bootstrap/provisioning contract: [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

Общая SSH/security policy: [`23-security.md`](23-security.md).

## 6. Template `9000`

Управляемые Debian VM создаются из общего базового template.

Template является универсальным OS baseline и не содержит credentials или application-specific state.

```text
template
→ Full Clone
→ guest-specific resources/network/public keys
→ first boot
→ provisioning
```

Паспорт: [`../templates/debian13/README.md`](../templates/debian13/README.md).  
Точный contract: [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## 7. Network architecture

Базовый L3/WAN/DHCP/firewall остаётся на физическом edge-router, чтобы отказ PVE не отключал основную сеть квартиры.

Отдельный network guest предоставляет расширенные server-side функции:

```text
DNS
VPN
PBR
remote-access VPN
optional mDNS reflection
```

Точные сегменты, addressing и routing boundary: [`40-network.md`](40-network.md).  
DNS contract: [`41-dns.md`](41-dns.md).  
IPv6 policy: [`42-ipv6.md`](42-ipv6.md).

## 8. AI и DevOps

AI Control и DevOps разделены:

```text
AI Control
→ agents + Proximo + AI SSH/Git identities

Dev services
→ Ansible + provisioning tooling + optional human UI
```

AI architecture contract: [`50-ai-control.md`](50-ai-control.md).  
Runbook ввода AI Control: [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md).

## 9. Filesystem и application data

Template не создаёт структуру будущих services заранее.

Provisioning размещает code/config/persistent data по общей Linux policy:

```text
/opt
/etc
/var/lib
/srv
/var/log
/var/cache
/run
```

Точные правила: [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md).

## 10. Backup и recovery

Git восстанавливает воспроизводимую configuration, но не заменяет backup persistent data и credentials.

```text
Policy
→ что резервировать, retention, RPO/RTO

Runbook
→ как выполнять restore-test и disaster recovery
```

Backup policy: [`22-storage-and-backup.md`](22-storage-and-backup.md).  
Recovery runbook: [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md).

## 11. Граница с проектом квартиры

В `proxmox` хранится серверная и эксплуатационная часть infrastructure.

Физическая сеть, кабели, размещение оборудования, камеры и apartment-level решения принадлежат `appart-rennovation`.

Граница двух репозиториев: [`60-apartment-infrastructure.md`](60-apartment-infrastructure.md).

## 12. Куда идти дальше

Полная карта типов документации и sources of truth: [`README.md`](README.md).

Главный принцип архитектуры:

> PVE lifecycle, guest configuration, AI control, network services, provisioning, backup и physical apartment infrastructure имеют отдельные boundaries и отдельные источники истины; Overview только связывает их между собой.
