# Документация 910 infra-manager

Этот каталог описывает, как службы и служебные механизмы устроены именно внутри `910 infra-manager`.

Общие правила проекта здесь не повторяются. Безопасность, развёртывание, резервное копирование и восстановление подробно описаны в основной документации проекта; локальные файлы дают ссылки на неё.

## Документы

| Файл | Что описывает |
|---|---|
| [`semaphore.md`](semaphore.md) | Semaphore: база, проект, задания, учётные данные, уровень вывода и проверка |
| [`openbao.md`](openbao.md) | OpenBao: Raft, разблокировка, рабочие секреты, SSH CA и переход к OTP |
| [`infra-runtime.md`](infra-runtime.md) | контейнер `infra-runtime`, его состав, подключения каталогов и безопасное самообновление |
| [`automation-tools.md`](automation-tools.md) | OpenTofu, Ansible, Packer, Git и Python как инструменты управления |
| [`system-services.md`](system-services.md) | службы systemd, порядок запуска 910, активация рабочей среды и служебные команды |
| [`data-and-access.md`](data-and-access.md) | постоянные данные 910, временные секреты, доступ к PVE и Git, границы резервирования |
| [`decisions.md`](decisions.md) | причины решений, относящихся именно к 910 |

## Как читать

Для общего понимания сначала откройте [`../README.md`](../README.md). Он является паспортом 910.

Если нужен конкретный вопрос, переходите сразу к нужной службе. Например:

```text
пароль и задания Semaphore     → semaphore.md
разблокировка OpenBao          → openbao.md
что находится в infra-runtime  → infra-runtime.md
где состояние OpenTofu         → automation-tools.md
что запускается вместе с 910   → system-services.md
что хранится на PVE            → data-and-access.md
```

## Источники точных значений

Документация объясняет устройство 910 человеку, но не заменяет машинные файлы.

Точные значения берутся из:

- [`../guest.yaml`](../guest.yaml) — параметры объекта Proxmox;
- [`../provision.yaml`](../provision.yaml) — требуемые пакеты, службы, версии, пути и постоянные данные;
- [`../status.yaml`](../status.yaml) — состав проверки и итогового экрана состояния;
- [`../rootfs/opt/infra-manager/compose/docker-compose.yml`](../rootfs/opt/infra-manager/compose/docker-compose.yml) — состав контейнеров и их подключения;
- [`../rootfs/etc/systemd/system/`](../rootfs/etc/systemd/system/) — службы и таймеры systemd;
- [`decisions.md`](decisions.md) — причины решений, относящихся именно к 910.

## Основная документация проекта

Особенности 910 опираются на общие документы:

- [`../../../../docs/300-guests/330-guest-lifecycle.md`](../../../../docs/300-guests/330-guest-lifecycle.md) — общий жизненный цикл гостей;
- [`../../../../docs/600-storage/610-backup.md`](../../../../docs/600-storage/610-backup.md) — резервное копирование;
- [`../../../../docs/700-security/710-pve-access.md`](../../../../docs/700-security/710-pve-access.md) — доступ 910 к PVE;
- [`../../../../docs/700-security/720-ssh-access.md`](../../../../docs/700-security/720-ssh-access.md) — SSH и доверие;
- [`../../../../docs/700-security/740-openbao.md`](../../../../docs/700-security/740-openbao.md) — нормативный контракт OpenBao;
- [`../../../../docs/700-security/750-access-contract.md`](../../../../docs/700-security/750-access-contract.md) — единый источник прав;
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — текущее состояние реализации безопасности;
- [`../../../../docs/800-operations/810-deployment.md`](../../../../docs/800-operations/810-deployment.md) — развёртывание;
- [`../../../../docs/800-operations/830-recovery.md`](../../../../docs/800-operations/830-recovery.md) — восстановление.
