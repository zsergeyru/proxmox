# Storage и резервное копирование

**Type:** Policy  
**Status:** Active  
**Source of truth:** Yes — для storage/backup принципов, retention, RPO/RTO и требований к восстановлению.

Этот документ отвечает на вопрос **«что и с какими гарантиями мы резервируем»**. Пошаговые процедуры восстановления находятся в [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md).

## 1. Основные принципы

- snapshot не заменяет backup;
- Git хранит воспроизводимую конфигурацию и код, но не заменяет backup persistent data;
- критичные данные должны иметь минимум две независимые backup-копии/места хранения;
- успешный backup job не считается достаточным без периодического restore-test;
- backup, содержащий private keys, tokens или другие secrets, считается чувствительным объектом;
- storage видеонаблюдения и backup не должны конкурировать за место без явных retention/space limits;
- то, что полностью воспроизводится из Git/bootstrap/Ansible, не должно становиться уникальным невоспроизводимым артефактом backup.

## 2. Текущая storage strategy

На текущем этапе:

```text
Proxmox
├── SMB/CIFS → отдельный компьютер — основная внешняя backup-копия
└── USB storage `backup`        — дополнительная локальная копия
```

NAS и Proxmox Backup Server пока не являются обязательной частью архитектуры. PBS имеет смысл вводить, когда появится практическая потребность в дедупликации, централизованном retention, incremental workflows или расширенной backup verification.

Фактическое состояние подключённых storage хранится в [`../host/pve/storage/README.md`](../host/pve/storage/README.md) и [`../host/pve/backup/README.md`](../host/pve/backup/README.md).

## 3. Классы backup

### Критичные гости

Критичными считаются production Home Assistant, network/control-plane после ввода в production и другие сервисы, потеря которых заметно нарушает работу дома.

Базовая цель:

```text
frequency:     daily
retention:     7 daily + 4 weekly
RPO target:    <= 24h
restore test:  не реже 1 раза в 3 месяца
```

### Некритичные и легко воспроизводимые гости

Для dev/test и простых сервисов:

```text
frequency:  2–3 раза в неделю
retention:  4–7 последних копий
```

Если guest полностью воспроизводим из template + Git + provisioning и не содержит уникального persistent state, частоту допустимо уменьшать.

## 4. RPO и RTO

Для каждого production-сервиса должны быть определены:

- **RPO** — сколько данных допустимо потерять;
- **RTO** — за какое время сервис должен быть восстановлен;
- какие данные требуют отдельного application-level backup помимо полного VM/LXC backup.

Базовый ориентир для критичных домашних сервисов:

```text
RPO <= 24 часа
RTO <= несколько часов при доступном backup и исправном PVE
```

Если сервис требует меньшего RPO, его persistent data резервируется чаще отдельным механизмом.

## 5. Что считается persistent data

Отдельного внимания требуют:

- Home Assistant backups/configuration;
- Docker volumes и базы данных;
- Git repositories и metadata;
- application state;
- persistent config/data, не воспроизводимые из Git;
- при необходимости Frigate recordings/metadata по собственной retention policy.

Размещение persistent data внутри Linux-гостей определяется [`34-linux-filesystem-layout.md`](34-linux-filesystem-layout.md).

## 6. Infrastructure credentials

Постоянные credentials PVE нельзя считать обычными пересоздаваемыми файлами.

К чувствительным объектам относятся как минимум:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/secrets/host-deploy.token
/etc/proxmox-deployer/secrets/ai-agent-infra.token
```

Их external backup должен быть защищён шифрованием и контролем доступа.

Особенно важен `pve_guest_ed25519`: его public half уже может быть установлен на существующих VM/LXC. Потеря private half означает отдельную процедуру recovery/rotation, а не тихую генерацию новой пары.

Общая credential policy определена в [`23-security.md`](23-security.md).

## 7. SMB/CIFS backup storage

Для внешнего SMB/CIFS storage требуется:

- стабильная доступность во время backup window;
- отдельная учётная запись с минимально необходимыми правами;
- мониторинг свободного места;
- независимость от системного диска PVE;
- практическая проверка восстановления.

## 8. USB backup storage

USB storage используется как дополнительная локальная копия и аварийный вариант.

До признания его надёжной частью backup policy должны быть подтверждены:

- automount после reboot PVE;
- стабильность device/storage mapping;
- успешное создание backup;
- успешный restore хотя бы тестового guest.

## 9. Restore verification policy

Для критичных систем restore-test обязателен не реже одного раза в три месяца.

Также внеочередной restore-test выполняется после существенных изменений:

- storage topology;
- backup tooling;
- encryption/credential handling;
- restore procedure;
- перехода на новый backup backend.

Процедура теста описана в [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md).

## 10. Полная потеря PVE host

Проект не требует хранить полный уникальный disk image самого PVE как единственный способ восстановления.

Целевая модель:

```text
чистый Proxmox
→ восстановить невоспроизводимые infrastructure credentials
→ повторно применить project bootstrap/configuration
→ подключить backup storage
→ восстановить критичные guests
→ восстановить остальные guests
→ verify
```

Host configuration, которая может быть безопасно воспроизведена проектом, должна восстанавливаться из Git/bootstrap. Невоспроизводимые credentials и данные резервируются отдельно.

`/etc/pve` не рассматривается как каталог, который нужно просто побайтно копировать обратно поверх новой установки. Конфигурация восстанавливается штатными средствами Proxmox и project automation.

## 11. Что должно быть проверяемым

Backup policy считается реализованной только если можно подтвердить:

```text
backup jobs выполняются
retention работает
свободное место контролируется
внешняя копия доступна
критичные credentials имеют защищённую внешнюю копию
restore-test фактически выполнялся
результат restore-test зафиксирован
```

Текущее observed состояние backup-инфраструктуры хранится в `host/pve/`, а не в этой policy.