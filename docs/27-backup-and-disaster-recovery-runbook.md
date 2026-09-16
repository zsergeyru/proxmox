# Backup restore и аварийное восстановление

**Type:** Runbook  
**Status:** Active  
**Source of truth:** Yes — для пошаговой проверки restore и восстановления PVE после потери host.

Backup requirements, retention и RPO/RTO определены в [`22-storage-and-backup.md`](22-storage-and-backup.md). Этот документ отвечает только на вопрос **«как проверить восстановление и что делать при аварии»**.

## 1. Restore-test одного guest

Для критичных систем restore-test выполнять не реже одного раза в три месяца и после существенных изменений backup/storage схемы.

Порядок:

1. выбрать актуальную backup-копию;
2. убедиться, что исходный production guest не будет затронут;
3. выбрать свободный временный VMID/CTID;
4. восстановить backup во временный объект или изолированную среду;
5. не подключать restored guest к production-сети, если существует риск конфликта IP/hostname/service identity;
6. запустить restored guest;
7. проверить загрузку ОС;
8. проверить ключевой application/service health;
9. проверить наличие ожидаемых persistent data;
10. зафиксировать результат, дату и фактическое время восстановления;
11. после успешной проверки остановить и удалить временный guest.

Restore-test считается успешным только если сервис реально запускается и необходимые данные доступны.

## 2. Что зафиксировать по результату restore-test

Минимальная запись:

```text
date
source backup
restored VMID/CTID
service checked
result: passed/failed
restore duration
problems found
follow-up actions
```

Если тест выявил проблему, исправить backup/restore chain и повторить проверку.

## 3. Полная потеря системного диска PVE

Базовый порядок восстановления:

```text
чистая установка Proxmox VE
→ вернуть hostname и базовую management network
→ восстановить permanent infrastructure credentials
→ запустить Public Bootstrap / PVE Configuration
→ проверить host configuration
→ подключить backup storage
→ убедиться, что backup-копии доступны
→ восстановить критичные VM/LXC
→ проверить критичные сервисы
→ восстановить остальные guests
→ проверить backup jobs
```

Runbook инициализации чистого host: [`20-pve-initialization.md`](20-pve-initialization.md).

## 4. Что восстановить до project bootstrap

На полностью новом host сначала требуется вернуть минимум, необходимый для безопасного запуска project automation:

- корректный hostname;
- management network и доступ к PVE;
- permanent credentials, которые нельзя получить повторно без ротации;
- доступ к GitHub/private project source по принятой bootstrap процедуре.

Точные project paths и ownership определены в [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md).

## 5. Infrastructure credentials

Особое внимание:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/secrets/host-deploy.token
/etc/proxmox-deployer/secrets/ai-agent-infra.token
```

### Если `pve_guest_ed25519` восстановлен

Продолжить штатный bootstrap/configuration и затем проверить host-side SSH к тестовому Debian guest.

### Если `pve_guest_ed25519` потерян

Не генерировать новую пару как незаметную замену старой.

Порядок:

```text
создать/зарегистрировать новую identity осознанно
→ определить guests со старым public key
→ добавить новый public key
→ проверить доступ новым private key
→ только после проверки удалить старый public key, если требуется
```

Это отдельная rotation/recovery операция.

### Если потерян GitHub Deploy Key

Создать новый Deploy Key, зарегистрировать public half на GitHub и восстановить canonical host credential по bootstrap policy. Старый key считается утраченным и отзывается.

## 6. Подключение backup storage

После восстановления host configuration:

1. подключить SMB/CIFS backup storage;
2. подключить/проверить USB backup storage, если он нужен;
3. убедиться, что Proxmox видит backup content;
4. проверить свободное место и права доступа;
5. не запускать production restore, пока backup source не проверен чтением/listing.

Фактические storage details должны браться из [`../host/pve/storage/README.md`](../host/pve/storage/README.md) и [`../host/pve/backup/README.md`](../host/pve/backup/README.md), а не из этого runbook.

## 7. Порядок восстановления guests

Рекомендуемый приоритет:

```text
1. критичная сеть/control-plane, если без них невозможно продолжение
2. production Home Assistant
3. остальные critical services
4. application/data services
5. monitoring
6. dev/test и легко воспроизводимые guests
```

Конкретный порядок может меняться в зависимости от зависимостей между сервисами.

Перед каждым restore проверить:

- правильный VMID/CTID;
- target storage;
- network config;
- отсутствие конфликта с уже существующим guest;
- наличие нужной backup version.

## 8. Проверка после восстановления host

Минимальный acceptance checklist:

```text
PVE доступен по management network
Public Bootstrap / PVE Configuration проходят успешно
canonical project checkout clean
storage доступны
project PVE identities/ACL работают
current template 9000 валиден
критичные guests запущены
критичные applications healthy
host-side SSH management работает
backup jobs снова включены
новый backup после восстановления создаётся успешно
```

## 9. Учебное восстановление

После существенного изменения bootstrap/backup architecture рекомендуется выполнить учебный сценарий на безопасной тестовой среде:

```text
чистый PVE
→ восстановить infrastructure credentials
→ выполнить bootstrap/configuration
→ подключить backup storage
→ восстановить тестовый VM/LXC
→ проверить boot/service/management access
→ создать новый backup
```

Цель такого теста — проверить всю цепочку, а не отдельный файл backup.

## 10. Когда остановиться

STOP и не выполнять destructive cleanup, если:

- неясно, какой backup является правильным;
- target VMID/CTID уже занят неизвестным объектом;
- storage mapping отличается от ожидаемого;
- credentials восстановлены частично и происхождение их неизвестно;
- восстановленный guest может конфликтовать с production по IP/hostname/service identity;
- нет способа проверить результат.

В неоднозначной ситуации сначала сохранить текущее состояние и разобраться в расхождении.