# Home Proxmox Infrastructure

Репозиторий конфигурации, кода и документации домашней инфраструктуры на Proxmox VE.

Этот файл — только точка входа. Он не дублирует подробные параметры VM/LXC, ACL, сети, template `9000` или bootstrap: для каждого из этих разделов есть отдельный источник истины.

## С чего начать

- [`docs/README.md`](docs/README.md) — карта документации и источников истины.
- [`guests/README.md`](guests/README.md) — как организованы VM/LXC и их manifests.
- [`host/pve/README.md`](host/pve/README.md) — наблюдаемое состояние работающего PVE host.
- [`scripts/README.md`](scripts/README.md) — карта исполняемого кода и проверок.
- [`templates/README.md`](templates/README.md) — VM/LXC templates.
- [`ansible/README.md`](ansible/README.md) — повторяемый provisioning Linux-гостей.

## Структура репозитория

```text
proxmox/
├── docs/        # архитектура, policy, specifications и runbooks
├── guests/      # desired state и файлы конкретных VM/LXC
├── host/pve/    # наблюдаемое состояние физического PVE host
├── scripts/     # bootstrap/configuration/validation tooling
├── templates/   # reusable VM/LXC templates и build assets
├── ansible/     # repeatable provisioning гостевых ОС
├── schemas/     # machine-readable guest/defaults/effective schemas
└── archive/     # исторические и заменённые материалы
```

Проект использует один основной monorepo. Гостевые системы не обязаны клонировать его целиком: в конкретную VM/LXC попадают только нужные ей файлы и конфигурация.

## Модель управления

```text
человек / host-side tooling
→ deploy-guest
→ deployer@pve!host-deploy
→ Proxmox guest lifecycle

AI Control
→ Proximo
→ ai-agent@pve!infra
→ managed guest lifecycle

311-dev-services
→ Ansible
→ repeatable provisioning Linux guests по SSH
```

Обзор участников и двух management flow: [`docs/26-deploy-guest-and-agent-access.md`](docs/26-deploy-guest-and-agent-access.md).

Точные PVE roles, privileges и ACL: [`docs/25-pve-access-control.md`](docs/25-pve-access-control.md).

## Источники истины

Источник истины выбирается **по предметной области**, а не по расположению файла или его номеру.

| Что определяется | Источник истины |
|---|---|
| Desired state VM/LXC | `guests/defaults.yaml` + `guests/<guest>/guest.yaml` |
| Формат и merge semantics manifests | [`docs/30-guest-manifest.md`](docs/30-guest-manifest.md) + `schemas/` |
| VMID/CTID и addressing plan | [`docs/11-vmid-plan.md`](docs/11-vmid-plan.md) |
| PVE initialization | [`docs/20-pve-initialization.md`](docs/20-pve-initialization.md) |
| Security и SSH identities | [`docs/23-security.md`](docs/23-security.md) |
| PVE API identities / roles / ACL | [`docs/25-pve-access-control.md`](docs/25-pve-access-control.md) |
| Guest bootstrap / provisioning boundary | [`docs/33-guest-bootstrap-and-provisioning.md`](docs/33-guest-bootstrap-and-provisioning.md) |
| Network policy | [`docs/40-network.md`](docs/40-network.md) |
| DNS policy | [`docs/41-dns.md`](docs/41-dns.md) |
| Debian VM template `9000` | [`templates/debian13/build-policy.md`](templates/debian13/build-policy.md) |
| Фактическое состояние PVE | `host/pve/` |

README конкретного гостя описывает его назначение и эксплуатационные особенности, ADR фиксирует устойчивое локальное решение, а `STATUS.md` — временный observed drift/status. Эти файлы не должны подменять `guest.yaml` как deploy desired state.

## Общие правила

- Git хранит воспроизводимую конфигурацию и документацию, но не runtime secrets.
- Passwords, tokens, private keys и другие секреты в репозиторий не добавляются.
- Для управляемых Debian VM/LXC используется `root` с public-key-only SSH; разные контуры имеют независимые SSH identities.
- PVE ACL, guest SSH и application permissions являются разными уровнями доступа.
- Опасные изменения должны иметь preflight, явную область воздействия и последующую verification.
- `archive/` не является источником действующей конфигурации.
