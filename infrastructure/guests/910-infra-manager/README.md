# 910 infra-manager

`910 infra-manager` — специальный постоянный LXC, из которого выполняется штатное развёртывание и сопровождение инфраструктуры проекта.

## Источники требований

Требования к 910 разделены по назначению:

| Источник | Что определяет |
|---|---|
| `guest.yaml` | постоянные параметры объекта 910, которые выражаются общей схемой гостя |
| `provision.yaml` | требуемые программы, службы, контейнеры, версии, постоянные пути, доступы и проверки внутри 910 |
| `compose/docker-compose.yml` | фактическую схему контейнера Semaphore |
| `compose/runtime/` | состав образа Semaphore с инфраструктурными инструментами |
| `decisions.md` | устойчивые решения и причины исключений |
| этот README | особый жизненный цикл, порядок запуска, эксплуатацию и восстановление |

`guest.yaml` использует общий профиль `debian-lxc-docker`. При этом VMID 910 закреплён за отдельным первоначальным состоянием OpenTofu: временный 990 создаёт 910, а постоянный OpenTofu-контур самого 910 всегда исключает его из собственного состояния.

Точный требуемый состав программ и служб хранится в `provision.yaml`; README не должен становиться вторым списком версий и пакетов.

910 не имеет отдельного штатного механизма настройки ОС. Его отличие от обычных гостей только в том, что объект Proxmox принадлежит отдельному состоянию 990. Внутри гостя применяется тот же общий путь `provision.yaml → Ansible`. Отдельный полный оркестратор настройки 910 удалён и запрещён архитектурным контрактом.

## Назначение

910 содержит только средства управления инфраструктурой:

- Semaphore;
- OpenTofu;
- Ansible;
- Packer;
- Git, Python, SSH и вспомогательные средства;
- постоянное состояние OpenTofu и служебные данные Semaphore.

Прикладные сервисы квартиры и домашнего сервера в 910 не размещаются.

## Объект Proxmox

Фиксированная часть контракта хранится в `guest.yaml`:

```text
VMID:          910
hostname:      infra-manager
тип:           unprivileged LXC
ОС:            Debian 13 amd64
CPU:           2
RAM:           4096 MB
swap:          1024 MB
диск:          32 GB
хранилище:     local-lvm
IPv4:          192.168.9.10/16
bridge:        vmbr0
onboot:        true
order:         10
startup delay: 30 s
protection:    true
pve_management:false
```

Профиль `debian-lxc-docker` задаёт тип LXC, источник Debian 13 и возможности контейнерного хоста. Фактическое имя файла LXC-шаблона разрешается через PVE API непосредственно перед запуском OpenTofu.

910 не включается в собственное постоянное состояние OpenTofu.

## Жизненный цикл

910 создаётся отдельным первоначальным контуром:

```text
PVE
└─ public bootstrap
   └─ создаёт временный LXC 990
      ├─ получает временный PVE API-доступ
      ├─ получает read-only доступ к проекту
      ├─ запускает infra-runtime
      └─ deploy-guest --bootstrap-scope 910
         ├─ OpenTofu создаёт LXC 910
         └─ общий Ansible playbook полностью применяет provision.yaml
            ├─ устанавливает требуемые пакеты и Docker
            ├─ создаёт постоянные каталоги
            ├─ разворачивает infra-runtime и Semaphore
            ├─ вызывает узкие служебные команды Python при необходимости
            ├─ разворачивает OpenBao после реализации его контракта
            └─ выполняет итоговые проверки
```

После подтверждения готовности 910 временные права 990 отзываются, его отдельное состояние OpenTofu удаляется вместе с контейнером и сам 990 удаляется.

При обычной эксплуатации 910 управляет остальными гостями, но не своим объектом Proxmox.

## Состав ОС

Точный список обязательных пакетов определён в `provision.yaml`.

В самом Debian находятся только базовые средства, необходимые для получения проекта, работы Docker и обслуживания 910. OpenTofu, Ansible и Packer не устанавливаются как отдельные системные службы Debian: они входят в образ `infra-runtime`.

Docker Engine устанавливается из официального репозитория Docker и должен быть включён и запущен через systemd.

## Контейнеры

### infra-runtime

Единый служебный контейнер:

```text
infra-runtime
```

Назначение:

- веб-интерфейс;
- проекты и шаблоны заданий;
- история запусков;
- расписания;
- хранение настроек и секретов Semaphore;
- непосредственное выполнение инфраструктурных заданий.

Отдельный remote Runner для одного домашнего LXC не используется. Когда внешние runners не настроены, Semaphore выполняет задания локально.

В первой версии используется SQLite. Контейнер работает в сети 910 напрямую, чтобы временный HTTP-сервер Packer был доступен устанавливаемой VM.

В `infra-runtime` находятся:

- Ansible;
- OpenTofu;
- Packer;
- Python;
- proxmoxer;
- Git;
- SSH-клиент;
- необходимые служебные программы.

OpenTofu state и другие постоянные данные не должны зависеть от жизненного цикла `infra-runtime`.

## Постоянные данные

Основные области:

```text
/etc/infra-manager/       конфигурация, CA и секреты
/var/lib/infra-manager/   постоянные данные и состояние
/opt/infra-manager/       разворачиваемая конфигурация Compose
/var/log/infra-manager/   журнал настройки
```

Обязательному резервному копированию подлежат как минимум:

```text
/etc/infra-manager/secrets/
/etc/infra-manager/ansible/
/var/lib/infra-manager/semaphore/
/var/lib/infra-manager/opentofu/state/
```

Критичны:

- база Semaphore;
- ключ шифрования Semaphore;
- PVE API credential;
- root SSH-ключ PVE;
- Semaphore API token;
- GitHub Deploy Key, используемый Semaphore;
- OpenTofu state.

Git checkout, образы контейнеров, Compose-файлы и кэш заданий считаются воспроизводимыми.

## OpenTofu state

Состояние хранится локально:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

State не хранится в Git и должен резервироваться.

Потеря state не должна приводить к автоматическому `apply` с пустым состоянием поверх существующей инфраструктуры.

## Доступ к PVE

910 является доверенным административным контуром домашнего PVE.

Используются два канала:

```text
HTTPS API
→ root@pam!infra-manager
→ privsep=0
→ OpenTofu и обычные операции PVE

root SSH
→ отдельный ключ infra-manager
→ операции PVE, которые запрещены API token
```

Отдельная матрица ACL для 910 больше не поддерживается.

Pool `managed` сохраняется как организационный объект и будущая граница для менее доверенных контуров, например AI Control. Он не ограничивает права infra-manager.

Для профиля `debian-lxc-docker` высокоуровневая возможность `container-host` по-прежнему означает:

```text
nesting=1
keyctl=1
```

`nesting` применяет OpenTofu через API. `keyctl` применяет общий host-only слой `infra_manager.pve_host` через root SSH. Специального кода только для VMID 910 нет.

Root SSH-идентичность хранится отдельно:

```text
/etc/infra-manager/pve-host/root_ed25519
/etc/infra-manager/pve-host/known_hosts
```

Она передаётся в `infra-runtime` только для чтения.

PVE CA устанавливается в доверенное хранилище 910; отключение проверки TLS не допускается.

## GitHub

Постоянный read-only Deploy Key создаётся публичным bootstrap на PVE:

```text
/root/.config/proxmox-bootstrap/github_proxmox_repo_ed25519
```

Копия передаётся внутрь 910 для чтения:

```text
git@github.com:zsergeyru/proxmox.git
```

При первом запуске открытый ключ добавляется в GitHub как Deploy Key без права записи.

Мягкое удаление 910 сохраняет исходный ключ на PVE, поэтому повторная регистрация в GitHub не требуется.

## Semaphore

Первая версия автоматически поддерживает только реально используемые объекты:

- проект `Proxmox Infrastructure`;
- ключ `GitHub project read-only`;
- репозиторий `proxmox`;
- Variable Group `OpenTofu PVE`;
- шаблон `OpenTofu Plan`;
- шаблон `Build Template 9000`;
- шаблон `Deploy Guest 410`.

Все инфраструктурные задания Semaphore выполняются как Python-сценарии из `scripts/infra-manager/jobs/`. Основная логика находится в Python-пакете `infra_manager`, а не в командных оболочках.

Постоянная SSH-идентичность Ansible хранится в `/etc/infra-manager/ansible/` и передаётся в `infra-runtime` только для чтения.

## Проверка

После настройки доступны:

```bash
infra-manager-status
infra-manager-status --full
infra-manager-pve-access-check
/usr/local/sbin/infra-manager-pve-lifecycle-test --apply

Semaphore:
- OpenTofu Plan
- Build Template 9000
- Deploy Guest 410
```

`infra-manager-status` проверяет локальное состояние 910, Docker, Semaphore, инфраструктурные инструменты и базовую авторизацию PVE API.

`--full` дополнительно проверяет окончательный контракт PVE-прав.

Тест жизненного цикла использует временный объект 9098 в `managed` и не должен затрагивать 910.

## Обновление

Повторный запуск нового первоначального контура:

- сохраняет постоянные данные;
- обновляет закрытый проект;
- сверяет объект 910 через OpenTofu;
- повторно применяет `provision.yaml` общим Ansible;
- пересобирает `infra-runtime` при необходимости;
- синхронизирует объекты Semaphore;
- выполняет итоговую проверку.

Настройка должна быть повторяемой.

## Восстановление

Если потерян PVE API secret, используется явный режим:

```bash
bootstrap-pve.sh --recover
```

Восстановление 910 должно сохранять или восстанавливать критичные постоянные данные. Новый пустой OpenTofu state поверх существующей инфраструктуры автоматически не применяется.

## Границы

910 не используется для:

- Home Assistant;
- MQTT;
- Zigbee2MQTT;
- ESPHome;
- Frigate;
- пользовательских приложений;
- общего файлового хранилища;
- иных прикладных сервисов, не относящихся к управлению инфраструктурой.

Komodo в текущий состав 910 не входит.

## Связанные документы

- [`guest.yaml`](guest.yaml) — параметры объекта 910.
- [`provision.yaml`](provision.yaml) — программы, службы и постоянное состояние.
- [`decisions.md`](decisions.md) — решения по 910.
- [`../../../docs/200-pve/210-host-bootstrap.md`](../../../docs/200-pve/210-host-bootstrap.md) — первоначальная подготовка.
- [`../../../docs/700-security/710-pve-access.md`](../../../docs/700-security/710-pve-access.md) — PVE API-доступ.
- [`../../../docs/800-operations/810-deployment.md`](../../../docs/800-operations/810-deployment.md) — процесс развёртывания.


## Сборка шаблона 9000

Штатная сборка запускается из Semaphore заданием `Build Template 9000`.

Semaphore использует сеть 910 напрямую, поэтому временный HTTP-сервер Packer доступен Debian Installer без отдельного proxy, второго контейнера или remote Runner.

Последовательность:

```text
проверка VMID 9000
→ актуальный Debian 13 netinst
→ Packer build
→ Cloud-Init defaults + protection
→ короткий Full Clone test через VMID 9099
```

Если VMID 9000 или 9099 занят неоднозначным объектом, сценарий останавливается без автоматического удаления.


## Вход в Semaphore

Аутентификация Semaphore остаётся включённой.

Первичный пароль создаётся один раз и сохраняется с правами `0600`:

```text
/etc/infra-manager/secrets/initial-admin-password
```

Общий Ansible не выводит пароль автоматически и не записывает его в журнал.

Получить пароль от root внутри 910 можно явно:

```bash
cat /etc/infra-manager/secrets/initial-admin-password
```

