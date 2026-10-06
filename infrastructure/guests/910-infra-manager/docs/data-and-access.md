# Данные и доступы 910 infra-manager

**Тип:** локальная спецификация данных
**Статус:** целевое состояние
**Назначение:** определить, какие данные 910 находятся на PVE, что подключается внутрь LXC, какие права и жизненный цикл имеют эти данные и что необходимо сохранить для полного восстановления управляющего контура.

## 1. Общая модель хранения

### Три области PVE

Канонический корень:

```text
/mnt/bindmounts/infra-manager/
├── pve-only/
├── access/
└── state/
```

Назначение:

| Область | Доступ из 910 | Назначение |
|---|---|---|
| `pve-only/` | нет | данные, которыми владеет только физический PVE и аварийный контур |
| `access/` | только чтение | доверенные идентичности и сертификаты, которые разрешено показать 910 |
| `state/` | чтение/запись | постоянное изменяемое состояние служб 910 |

Внутри LXC:

```text
access/ → /mnt/pve-access        RO
state/  → /mnt/persistent-state RW
```

Если bind mount отсутствует, Ansible должен завершиться ошибкой. Создавать обычный локальный каталог вместо ожидаемого подключения запрещено.

### Что не хранится в rootfs LXC

Корневая файловая система 910 не является единственным местом хранения:

- OpenBao Raft;
- Semaphore;
- OpenTofu state;
- PVE-only secrets;
- TLS CA OpenBao.

Пересоздание LXC не должно уничтожать эти данные.

## 2. PVE-only и область access

### PVE-only OpenBao

Обязательная структура:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao/
├── unseal.key
├── ssh-access.json
└── kv-access.json
```

Эти файлы:

- не подключаются внутрь 910;
- не хранят root token;
- резервируются как критичные данные восстановления;
- используются только доверенными PVE-only операциями.

### TLS CA OpenBao

Закрытый TLS CA для сетевого OTP-входа OpenBao также принадлежит PVE:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao-tls/
├── ca.key
└── ca.crt
```

`ca.key` никогда не подключается в 910.

Серверный комплект, который можно показать 910:

```text
/mnt/bindmounts/infra-manager/access/openbao-tls/
├── ca.crt
├── server.crt
└── server.key
```

Внутри LXC:

```text
/mnt/pve-access/openbao-tls/
```

Целевая установка для OpenBao:

```text
/etc/infra-manager/openbao/tls/
├── ca.crt
├── server.crt
└── server.key
```

Требуемые права:

```text
ca.crt      root:root 0644
server.crt  root:root 0644
server.key  root:root 0600
```

Каталог TLS доступен контейнеру OpenBao только для чтения.

### Root SSH 910 → PVE

В `access/` должны находиться:

```text
pve-host/root_ed25519
pve-host/known_hosts
```

Закрытый ключ доступен только тому процессу 910, которому нужен PVE root SSH.

Целевые права внутри доступного 910 представления:

```text
root_ed25519  1001:0 0600
known_hosts   1001:0 0644
```

Этот ключ не используется для Git или Linux-гостей.

### CA PVE

Ожидаемый файл:

```text
/mnt/pve-access/ca/pve-root-ca.crt
```

Он должен устанавливаться в системное доверие 910 и входить в:

```text
/etc/infra-manager/ca/ca-bundle.crt
```

## 3. Постоянное состояние служб

### Основные каталоги

Целевая структура:

```text
/mnt/persistent-state/
├── semaphore/
├── opentofu/
└── openbao/
```

Постоянная `state/ansible/guest_ed25519` **не является частью целевой схемы**. Она относится к переходному состоянию и должна исчезнуть после полного перехода административного SSH на короткоживущие client certificates.

Требуемые каталоги:

| Путь | Владелец | Режим | Назначение |
|---|---:|---:|---|
| `/mnt/persistent-state/semaphore` | `1001:0` | `0770` | SQLite, история и данные Semaphore |
| `/mnt/persistent-state/opentofu` | `1001:0` | `0750` | рабочие данные и state OpenTofu |
| `/mnt/persistent-state/openbao` | `root:root` | `0700` | Raft OpenBao |

OpenTofu state:

```text
/mnt/persistent-state/opentofu/state/proxmox.tfstate
```

Рабочая ссылка:

```text
/var/lib/infra-manager/opentofu
→ /mnt/persistent-state/opentofu
```

Semaphore:

```text
/var/lib/infra-manager/semaphore
→ /mnt/persistent-state/semaphore
```

OpenBao:

```text
/var/lib/persistent/openbao
→ /mnt/persistent-state/openbao
```

### Подготовка Ansible

Роль `infra_manager` должна:

1. проверить, что `/mnt/pve-access` и `/mnt/persistent-state` являются mount;
2. проверить старые пути перед созданием ссылок;
3. не удалять неизвестные данные;
4. создать необходимые каталоги с точными владельцами;
5. создать только ожидаемые ссылки;
6. завершиться ошибкой при неожиданном источнике ссылки или неизвестном непустом каталоге.

Это защищает от тихой потери состояния при миграции.

## 4. Временные и воспроизводимые данные

### Рабочие секреты

Временная область:

```text
/run/infra-manager/secrets/
```

Целевой набор:

```text
pve-api.env
github_proxmox_repo_ed25519
semaphore-server.env
initial-admin-password
semaphore-api-token
.openbao-materialized
```

Требования:

- каталог `root:root 0750`;
- secret-файлы с минимальными правами;
- `infra-runtime` получает каталог только для чтения;
- после перезапуска данные восстанавливаются из OpenBao;
- содержимое `/run` не резервируется как постоянное состояние.

### Bootstrap secrets

Только до первичной инициализации OpenBao допускается:

```text
/run/infra-manager/bootstrap-secrets/
```

После переноса данных в OpenBao этот каталог не является штатным источником.

### Машинный AppRole гостя

RoleID/SecretID для OTP-источников не входят в общий каталог 910 и не должны храниться как центральный файл со всеми гостями.

Они доставляются только соответствующей машине по спецификации [`openbao.md`](openbao.md).

### Воспроизводимые данные 910

Из Git и машинных описаний должны восстанавливаться:

```text
/var/lib/infra-manager/bootstrap-repo/
/opt/infra-manager/compose/
/usr/local/lib/infra-manager/
/usr/local/sbin/infra-manager-*
/etc/infra-manager/status.yaml
/etc/systemd/system/infra-manager-*.service
Docker images
кэши
```

Их потеря сама по себе не требует восстановления резервной копии.

## 5. Резервирование и восстановление

### Что обязательно сохранять

| Данные | Область | Можно создать заново без потери доверия |
|---|---|---|
| OpenBao Raft | `state/openbao` | нет |
| OpenBao unseal/AppRole PVE | `pve-only/openbao` | не как обычное восстановление |
| TLS CA OpenBao | `pve-only/openbao-tls` | нет без смены доверия |
| TLS server material | `access/openbao-tls` | да, если сохранён тот же TLS CA |
| Semaphore | `state/semaphore` | прежнее состояние — нет |
| OpenTofu state | `state/opentofu/state` | нет поверх существующей инфраструктуры |
| PVE root SSH / CA | `access/` | только через отдельную ротацию/выдачу |
| Compose/Python/commands | Git / rootfs | да |
| `/run/infra-manager/secrets` | временно | да, из OpenBao |

Общая политика резервного копирования определяется разделом `600`.

### Восстановление после пересоздания LXC

Порядок:

1. на PVE проверить сохранность `pve-only`, `access` и `state`;
2. подключить `access` к `/mnt/pve-access` как RO;
3. подключить `state` к `/mnt/persistent-state` как RW;
4. не создавать локальные замены mount;
5. установить базовую систему и Docker;
6. применить Ansible 910;
7. установить TLS material OpenBao;
8. запустить и разблокировать OpenBao;
9. восстановить рабочие секреты;
10. восстановить Semaphore и `infra-runtime`;
11. проверить OpenTofu state;
12. синхронизировать OTP-контракт;
13. выполнить с PVE `infra-manager status`.

Если ожидаемый старый каталог содержит неизвестные данные, автоматизация должна остановиться до ручного решения.

## 6. Проверка и типичные ошибки

### Проверка mount и состояния

```bash
mountpoint /mnt/pve-access
mountpoint /mnt/persistent-state
stat /mnt/persistent-state/semaphore
stat /mnt/persistent-state/opentofu
stat /mnt/persistent-state/openbao
```

Проверить ссылки:

```bash
readlink -f /var/lib/infra-manager/semaphore
readlink -f /var/lib/infra-manager/opentofu
readlink -f /var/lib/persistent/openbao
```

Проверить access:

```bash
test -s /mnt/pve-access/pve-host/root_ed25519
test -s /mnt/pve-access/pve-host/known_hosts
test -s /mnt/pve-access/ca/pve-root-ca.crt
test -s /mnt/pve-access/openbao-tls/ca.crt
test -s /mnt/pve-access/openbao-tls/server.crt
test -s /mnt/pve-access/openbao-tls/server.key
```

Проверить рабочие секреты:

```bash
test -s /run/infra-manager/secrets/pve-api.env
test -s /run/infra-manager/secrets/github_proxmox_repo_ed25519
test -s /run/infra-manager/secrets/semaphore-server.env
```

Полная проверка:

```bash
infra-manager-status --full
```

### Ошибки

Если `mountpoint` сообщает ошибку — исправить bind mount на PVE, а не создавать локальный каталог.

Если Ansible отказывается заменить старый путь ссылкой — сначала проверить данные и выполнить осознанную миграцию.

Если нет `pve-api.env` — проверить OpenBao startup-unseal и материализацию KV.

Если `infra-runtime` не может писать SQLite или OpenTofu state — проверить числовых владельцев `1001:0` и режимы каталогов.

Если TLS listener OpenBao не запускается — проверить наличие `server.key/server.crt`, SAN сертификата, права файлов и read-only mount в контейнер.

## 7. Источники и связанные документы

Машинные источники после полной реализации:

- [`../provision.yaml`](../provision.yaml) — bindings, владельцы и режимы;
- `rootfs/` — воспроизводимые файлы 910;
- Ansible-роль `infra_manager` — создание каталогов, ссылок и установка TLS material.

Связанные документы:

- [`openbao.md`](openbao.md) — Raft, TLS, PVE-only данные, KV и OTP;
- [`semaphore.md`](semaphore.md) — постоянная база Semaphore;
- [`infra-runtime.md`](infra-runtime.md) — подключения контейнеров;
- [`automation-tools.md`](automation-tools.md) — OpenTofu state;
- [`../../../../docs/200-pve/230-host-layout.md`](../../../../docs/200-pve/230-host-layout.md) — общая структура PVE;
- [`../../../../docs/600-storage/610-backup.md`](../../../../docs/600-storage/610-backup.md) — общая политика резервирования;
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — переходные отличия текущего кода.
