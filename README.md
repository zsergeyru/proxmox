# Домашняя инфраструктура Proxmox

Репозиторий конфигурации, кода и документации домашней инфраструктуры на Proxmox VE.

Этот файл — только точка входа. Он не дублирует подробные параметры VM/LXC, ACL, сети, шаблона `9000` или первоначальной настройки: для каждого раздела есть отдельный основной источник.

## С чего начать

- [`docs/README.md`](docs/README.md) — карта документации, типов документов и основных источников.
- [`guests/README.md`](guests/README.md) — как организованы VM/LXC и их манифесты.
- [`host/pve/README.md`](host/pve/README.md) — фактическое состояние работающего PVE-хоста.
- [`scripts/README.md`](scripts/README.md) — карта исполняемого кода и проверок.
- [`templates/README.md`](templates/README.md) — шаблоны VM/LXC.
- [`ansible/README.md`](ansible/README.md) — повторяемая настройка Linux-гостей.

## Структура репозитория

```text
proxmox/
├── docs/        # Обзор / Политика / Спецификация / Справочник / Инструкция
├── guests/      # требуемое состояние и файлы конкретных VM/LXC
├── host/pve/    # фактическое состояние физического PVE-хоста
├── scripts/     # средства первоначальной настройки, конфигурации и проверки
├── templates/   # повторно используемые шаблоны VM/LXC и файлы их сборки
├── ansible/     # повторяемая настройка гостевых ОС
├── schemas/     # машинные схемы guest/defaults/effective
└── archive/     # исторические и заменённые материалы
```

Проект использует один основной репозиторий. Гостевые системы не обязаны клонировать его целиком: в конкретную VM/LXC попадают только нужные ей файлы и конфигурация.

## Модель управления

```text
человек / инструменты на стороне PVE-хоста
→ deploy-guest
→ deployer@pve!host-deploy
→ жизненный цикл гостевых систем Proxmox

AI Control
→ Proximo
→ ai-agent@pve!infra
→ жизненный цикл разрешённых гостевых систем managed

311-dev-services
→ Ansible
→ повторяемая настройка Linux-гостей по SSH
```

Обзор участников и двух путей управления: [`docs/26-deploy-guest-and-agent-access.md`](docs/26-deploy-guest-and-agent-access.md).

Точные роли, привилегии и ACL PVE: [`docs/25-pve-access-control.md`](docs/25-pve-access-control.md).

## Основные источники

Основной источник выбирается **по предметной области**, а не по расположению файла или его номеру.

| Что определяется | Основной источник |
|---|---|
| Требуемое состояние VM/LXC | `guests/defaults.yaml` + `guests/<guest>/guest.yaml` |
| Формат манифестов и правила объединения настроек | [`docs/30-guest-manifest.md`](docs/30-guest-manifest.md) + `schemas/` |
| VMID/CTID и правила адресации | [`docs/11-vmid-plan.md`](docs/11-vmid-plan.md) |
| Процедура инициализации PVE | [`docs/20-pve-initialization.md`](docs/20-pve-initialization.md) |
| Пути, рабочие данные и файлы состояния PVE | [`docs/21-pve-filesystem-layout.md`](docs/21-pve-filesystem-layout.md) |
| Политика резервного копирования, сроков хранения, RPO/RTO | [`docs/22-storage-and-backup.md`](docs/22-storage-and-backup.md) |
| Восстановление и аварийное восстановление | [`docs/27-backup-and-disaster-recovery-runbook.md`](docs/27-backup-and-disaster-recovery-runbook.md) |
| Безопасность, SSH-ключи и общий Project Git READ credential | [`docs/23-security.md`](docs/23-security.md) |
| Учётные записи API PVE, роли и ACL | [`docs/25-pve-access-control.md`](docs/25-pve-access-control.md) |
| Первичная и повторяемая настройка гостевых систем | [`docs/33-guest-bootstrap-and-provisioning.md`](docs/33-guest-bootstrap-and-provisioning.md) |
| Сетевая спецификация | [`docs/40-network.md`](docs/40-network.md) |
| Спецификация DNS | [`docs/41-dns.md`](docs/41-dns.md) |
| Шаблон Debian VM `9000` | [`templates/debian13/build-policy.md`](templates/debian13/build-policy.md) |
| Фактическое состояние PVE | `host/pve/` |

README конкретной гостевой системы описывает её назначение и эксплуатационные особенности, ADR фиксирует устойчивое локальное решение, а `STATUS.md` — временное фактическое состояние или расхождение. Эти файлы не должны подменять `guest.yaml` как источник требуемого состояния для развёртывания.

## Общие правила

- Git хранит воспроизводимую конфигурацию и документацию, но не рабочие секреты.
- Пароли, токены, закрытые ключи и другие секреты в репозиторий не добавляются.
- Для управляемых Debian VM/LXC используется `root` с SSH-доступом только по открытому ключу; разные административные контуры имеют независимые пары SSH-ключей.
- Для чтения закрытого `zsergeyru/proxmox` допускается один общий GitHub Deploy Key только для чтения; это отдельный прикладной credential, не административный SSH-ключ.
- Принятый целевой manifest-интерфейс для его выдачи гостю — `access.project_repo_read: true`; до реализации соответствующей schema/resolver это поле не добавляется в действующие `guest.yaml`.
- ACL PVE, SSH-доступ внутрь гостевой системы, read-only доступ к проектному Git и права приложений являются разными уровнями доступа.
- Опасные изменения должны иметь предварительные проверки, явную область воздействия и последующую проверку результата.
- `archive/` не является источником действующей конфигурации.
