# Домашняя инфраструктура Proxmox

Репозиторий хранит требуемое состояние домашней инфраструктуры Proxmox VE, средства её развёртывания и документацию.

Постоянным управляющим гостем является `910 infra-manager`. Он создаётся временным первоначальным контуром `990 bootstrap-runner`, после чего управляет остальными гостями.

## Штатный первоначальный путь

Публичный вход разделён на два слоя:

```text
bootstrap-pve.sh
→ bootstrap-pve.py
→ временный LXC 990 bootstrap-runner
→ read-only Deploy Key
→ clone закрытого zsergeyru/proxmox
→ scripts/bootstrap-runner/bootstrap-host.py
```

`bootstrap-pve.sh` является только коротким загрузчиком Python-файла. На первом запуске `bootstrap-pve.py` создаёт Deploy Key и завершает работу до создания 990, чтобы ключ можно было добавить в GitHub.

После передачи управления вся логика PVE API, OpenTofu, 910, Ansible, повторного запуска и очистки выполняется закрытым проектом. Публичный репозиторий не содержит политики PVE-доступа и не знает устройство 910.

## Главный принцип

Физический PVE остаётся максимально чистым.

На PVE постоянно не устанавливаются:

- Docker;
- Semaphore;
- OpenTofu;
- Ansible;
- Packer;
- прикладные службы проекта.

Постоянные средства управления работают внутри 910.

## Общая архитектура

```text
GitHub public: zsergeyru/proxmox-bootstrap
        │
        ▼
физический PVE
        │
        ├── read-only Deploy Key проекта
        └── временный LXC 990
                │
                ├── clone закрытого проекта
                └── private bootstrap-host.py
                        │
                        ├── временный PVE API token
                        ├── bootstrap-runtime
                        ├── OpenTofu
                        ├── Ansible
                        └── отдельное состояние только для 910
                        │
                        ▼
                LXC 910 infra-manager
                        │
                        ├── Docker
                        └── infra-runtime
                            ├── Semaphore
                            ├── OpenTofu
                            ├── Ansible
                            ├── Packer
                            └── proxmoxer
                                 │
                                 ▼
                              PVE API
```

После подтверждения готовности 910 временный 990 и его права должны быть удалены.

## Единый путь настройки гостей

Для Linux-гостей используется один механизм:

```text
guest.yaml
→ OpenTofu
→ provision.yaml
→ общий Ansible playbook
```

910 не имеет отдельного механизма настройки ОС.

Его единственное архитектурное отличие — объект Proxmox 910 принадлежит отдельному состоянию временного 990. Для разрушительных операций 910 дополнительно имеет строгую метку владения `owner=proxmox-project;role=infra-manager` в описании Proxmox. Постоянное состояние OpenTofu внутри 910 никогда не содержит ресурс 910.

Старый отдельный `setup.sh` удалён.

## Первоначальное создание 910

Текущая последовательность новой схемы:

```text
PVE
→ создать 990
→ подготовить временный PVE API-доступ 990
→ получить закрытый проект
→ подготовить bootstrap-runtime
→ deploy-guest 910 --infrastructure-only
→ общий Ansible: базовая часть provision.yaml
→ передать в 910 PVE CA, постоянный PVE token и GitHub-доступ
→ общий Ansible: полностью применить provision.yaml
→ infra-manager-status --full
```

Временный OpenTofu state 990 обязан содержать ровно один ресурс:

```text
proxmox_virtual_environment_container.guest["910"]
```

## LXC 910 infra-manager

Основной контракт хранится в:

```text
infrastructure/guests/910-infra-manager/guest.yaml
```

Текущие параметры:

```text
VMID:          910
hostname:      infra-manager
тип:           unprivileged LXC
ОС:            Debian 13 amd64
CPU:           2
RAM:           4096 MiB
swap:          1024 MiB
диск:          32 GiB
хранилище:     local-lvm
IPv4:          192.168.9.10/16
bridge:        vmbr0
onboot:        yes
order:         10
startup delay: 30 s
protection:    yes
```

910 использует общий профиль `debian-lxc-docker` и имеет:

```yaml
pve_management: false
```

Это исключает 910 из собственного постоянного состояния OpenTofu.

Подробнее: `infrastructure/guests/910-infra-manager/README.md`.

## provision.yaml 910

Требуемое содержимое гостя задаёт:

```text
infrastructure/guests/910-infra-manager/provision.yaml
```

Он описывает:

- системные пакеты;
- Docker;
- постоянные каталоги;
- `infra-runtime`;
- Semaphore;
- версии OpenTofu и Packer;
- постоянные данные;
- пути PVE/GitHub-доступов;
- обязательные проверки.

Общий Ansible применяет этот контракт. Сложные изолированные операции могут выполняться узкими Python-командами, например синхронизацией объектов Semaphore.

## Постоянные данные 910

Основные области:

```text
/etc/infra-manager/
/var/lib/infra-manager/
/opt/infra-manager/
```

Критичные данные:

```text
/etc/infra-manager/secrets/
/etc/infra-manager/ansible/
/var/lib/infra-manager/semaphore/
/var/lib/infra-manager/opentofu/state/
```

Постоянный OpenTofu state:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Он не хранится в Git и должен резервироваться.

## Доступ 910 к PVE

Постоянная идентичность:

```text
root@pam!infra-manager
privsep=1
```

Политика прав находится только в:

```text
scripts/infra-manager/pve-bootstrap-access.sh
```

Постоянный root SSH с 910 на PVE не используется.

## GitHub Deploy Key

Первоначальный read-only ключ проекта хранится на PVE:

```text
/root/.config/proxmox-bootstrap/github_proxmox_repo_ed25519
```

Он необходим для восстановления 910 из закрытого репозитория ещё до появления постоянного хранилища секретов внутри 910.

Копия ключа передаётся в 910 для Semaphore.

## Semaphore и infra-runtime

Постоянный контейнер:

```text
infra-runtime
```

содержит:

- Semaphore;
- OpenTofu;
- Ansible;
- Packer;
- Python;
- proxmoxer;
- Git;
- SSH-клиент.

Первая версия использует SQLite и локальное выполнение заданий Semaphore.

Автоматически поддерживаются:

- проект `Proxmox Infrastructure`;
- Git-репозиторий `proxmox`;
- ключ `GitHub project read-only`;
- набор переменных `OpenTofu PVE`;
- `OpenTofu Plan`;
- `Build Template 9000`;
- `Deploy Guest 410`.

## Первичный пароль Semaphore

Пароль создаётся один раз и хранится с правами `0600`:

```text
/etc/infra-manager/secrets/initial-admin-password
```

Он не выводится автоматически общим Ansible-процессом и не должен попадать в журнал.

Получить его от root внутри 910 можно явно:

```bash
cat /etc/infra-manager/secrets/initial-admin-password
```

## Служебные команды 910

```bash
infra-manager-status
infra-manager-status --full
infra-manager-pve-access-check
/usr/local/sbin/infra-manager-pve-lifecycle-test --apply
```

## Основные каталоги проекта

```text
proxmox/
├── docs/               документация
├── infrastructure/     требуемое состояние
│   ├── guests/         описания VM/LXC
│   ├── host/           состояние PVE-хоста
│   └── schemas/        схемы проверки
├── automation/
│   ├── opentofu/       виртуальные объекты PVE
│   ├── packer/         базовые шаблоны VM
│   └── ansible/        настройка Linux-гостей
└── scripts/            задания, проверки и служебная логика
```

## Связанные документы

- `docs/200-pve/210-host-bootstrap.md` — первоначальный контур 990;
- `docs/300-guests/320-guest-manifest.md` — контракт `guest.yaml`;
- `docs/700-security/710-pve-access.md` — права 910;
- `docs/800-operations/810-deployment.md` — порядок развёртывания;
- `infrastructure/guests/910-infra-manager/README.md` — паспорт 910.

## Общие правила

- секреты не хранятся в Git;
- PVE остаётся минимальным гипервизором;
- 910 не управляет собственным объектом через постоянный OpenTofu state;
- настройка Linux-гостей выполняется общим Ansible-механизмом;
- новые права выдаются только под реально используемую функцию;
- `archive/` и устаревшая документация не являются источниками действующей конфигурации.
