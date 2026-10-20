# Рабочая среда infra-runtime без Semaphore

**Тип:** инструкция по эксплуатации управляющего LXC 910
**Статус:** код нового контура находится в `main`; переход на работающем 910 ещё требует реальной проверки.
**Назначение:** описать локальную сборку, безопасное самообновление и диагностику.

## 1. Архитектура

- `infra-runtime`: Python 3.12, Ansible, OpenTofu, Packer и рабочие задания; UID 1001, GID 0.
- `openbao`: отдельный Docker-контейнер, постоянный Raft и машинное доверие.
- `infra-web`: отдельный HTTPS-интерфейс с авторизацией через OpenBao.
- `systemd` внутри LXC 910: независимая от Docker отложенная активация нового поколения.

**Semaphore не нужен:** его контейнер, база SQLite, API, пользователи и шаблоны не участвуют в новом контуре. Данные старого Semaphore **не мигрируются**. Их можно удалить **после подтверждения** работоспособности нового runtime и OpenBao; штатный `deploy` не стирает их при неуспехе.

Текущий образ описан в
[`runtime-next/Dockerfile`](../rootfs/opt/infra-manager/compose/runtime-next/Dockerfile).
Compose нового поколения:
[`docker-compose-standalone.yml`](../rootfs/opt/infra-manager/compose/docker-compose-standalone.yml).
Ansible собирает образ локально с проверкой источников загрузки и закреплённых версий из `provision.yaml`; архив Docker-образа на PVE не хранится.

## 2. Обязательные подключения

| Источник на 910 | Путь в infra-runtime | Режим |
|---|---|---|
| `/mnt/persistent-state/operations/activation` | `/run/infra-manager/activation` | RW |
| `/mnt/persistent-state/locks` | `/var/lib/infra-manager/locks` | RW |
| `/mnt/persistent-state/opentofu` | `/var/lib/infra-manager/opentofu` | RW |
| `/mnt/persistent-state/physical-hosts` | `/var/lib/infra-manager/physical-hosts` | RW |
| `/var/lib/infra-manager/bootstrap-repo` | тот же путь | RO |
| `/run/infra-manager/secrets` | тот же путь | RO |
| `/etc/infra-manager/ca` | тот же путь | RO |
| `/mnt/pve-access/pve-host` | тот же путь | RO |

Постоянное состояние находится на PVE под `/mnt/bindmounts/infra-manager/state` и должно быть подтверждено как bind mount **до создания каталогов**. Нельзя заменять отсутствующее монтирование пустой директорией в rootfs.

Git-копия может быть недоступна UID 1001 напрямую из-за прав `0700` у root. Оператор копирует её в собственный временный снимок через `docker exec --user 0`, а снимок передаёт UID 1001; сам `guest-operation.py` работает только из этого снимка.

## 3. Первое обновление старого 910

Работающий до обновления Docker-контейнер может ещё иметь прежние монтирования и **не иметь** `/run/infra-manager/activation`. При этом `state/locks` уже подключён в него как общий bind mount.

Для первой смены поколения предусмотрен переходный файл **без Semaphore**:

```text
на 910:         /mnt/persistent-state/locks/runtime-activation-pending
в старом runtime: /var/lib/infra-manager/locks/runtime-activation-pending
```

Новый `guest-operation.py` при отсутствии смонтированного каталога операций выбирает этот общий каталог. Если ни `operations/activation`, ни `locks` не подключены, запуск прекращается без создания подменного состояния.

Во время штатного `infra-manager deploy 910`:

1. Оператор проверяет объект 910, обновляет Git и создаёт снимок для работающего задания.
2. Исполнитель резервирует маркер с пространством PID (`runtime:<PID>`). Новый оператор дополнительно ведёт записи с идентификатором операции; установленный старый оператор ещё может обходиться без них.
3. Ansible устанавливает Python-код и службы, готовит каталог `state/operations`, собирает образ без Semaphore и обновляет конфигурацию. **Запущенный `infra-runtime` не пересоздаётся из своего задания.**
4. Playbook через `systemd-run` запускает `infra-manager-activate-runtime` внутри LXC.
5. Служба находит маркер в `operations/activation` либо в `state/locks`, дожидается завершения исходного процесса через `docker exec --user 0`, только затем переключает Compose на новое поколение.
6. После переключения выполняются восстановление/проверка OpenBao и `infra-manager-status --full --quiet`. Успех не объявляется только по факту запуска контейнера.
7. Служба записывает результат в постоянное хранилище операций, если был выдан идентификатор, и снимает использованный маркер после обработки завершения.

**После первого переключения** новый `infra-runtime` использует нормальный маршрут `state/operations/activation`; переход через `state/locks` больше не требуется. Удалять `state/locks` нельзя: здесь находятся также рабочие блокировки других операций.

В автоматическом режиме старые данные Semaphore не удаляются до полной проверки, но **никакой миграции старой SQLite-базы, заданий, учётных записей и истории не требуется**.

## 4. Проверка и диагностика

С PVE:

```bash
infra-manager status 910
pct exec 910 -- docker ps --format '{{.Names}} {{.Status}}'
pct exec 910 -- docker inspect --format '{{range .Mounts}}{{println .Destination .Source}}{{end}}' infra-runtime
pct exec 910 -- tail -n 100 /var/log/infra-manager/runtime-activation.log
```

Из LXC 910:

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime ansible --version
docker exec infra-runtime packer version
systemctl list-units 'infra-manager-runtime-activate-*'
```

Если `deploy 910` завершился до отложенной активации, окончательную готовность подтверждает системная служба и последующий `status`. При ошибке нельзя немедленно повторять `deploy` или удалять маркер без проверки работающего задания.

При отсутствующем общем монтировании выводится понятное сообщение и безопасная техническая диагностика (код системной ошибки без секретов). Журнал активации имеет закрытые права в `/var/log/infra-manager/runtime-activation.log`. Сырые аргументы и секреты в пользовательском выводе не публикуются.

## 5. Восстановление

Для штатных операций используется только 910. Временный 990 нужен **только** для первоначальной установки или аварийного пересоздания управляющего LXC через `infra-manager recover`, а не для обычного обновления.

При восстановлении не создавать пустой Raft, OpenTofu state, SSH CA или `physical-hosts` вместо прежних данных. Прежний Semaphore не восстанавливать.

Связанные документы: [самообновление](self-maintenance.md), [контракт активации](activation-contract.md), [данные и доступы](data-and-access.md), [системные службы](system-services.md).
