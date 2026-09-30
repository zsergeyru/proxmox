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

`bootstrap-pve.sh` является только короткой оболочкой для получения и запуска Python-файла. На первом запуске `bootstrap-pve.py` создаёт Deploy Key и завершает работу до создания 990, чтобы ключ можно было добавить в GitHub.

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
                                 ├── PVE API
                                 └── root SSH для host-only операций
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

## Итог успешного развёртывания

Для 910 финальный экран является обычным полным состоянием самого `infra-manager`:

```bash
infra-manager-status --full
```

Состав проверок, источники данных, подписи и секции экрана задаёт `infrastructure/guests/910-infra-manager/status.yaml`. `scripts/infra-manager/infra_manager/status.py` только выполняет разрешённые проверки, получает фактические значения и печатает описанный экран. Поэтому тот же экран доступен в любой момент без запуска временного 990:

```bash
pct exec 910 -- infra-manager-status --full
```

Перед удалением 990 первоначальный контур выполняет эту же полную проверку без вывода. После удаления 990 она запускается открыто и становится единственным финальным экраном установки. Пароль Semaphore выводится только в открытом полном состоянии и не попадает во внутреннюю проверку Ansible.

Обычное полное развёртывание гостя через `deploy-guest`, например VM 410, завершается общим итогом из `scripts/infra-manager/infra_manager/guest_deploy.py`. Он показывает VMID, имя, тип, адрес, узел, ветку и версию проекта.

## Обычное обновление 910

После первоначального создания 990 для обычных обновлений не нужен. В Semaphore используется штатное задание:

```text
Deploy Guest 910
```

Оно применяет общий `deploy-guest → Ansible`, но не включает собственный объект 910 в постоянное состояние OpenTofu. Перезапуск `infra-runtime` откладывается до завершения задания, чтобы Semaphore не остановил сам себя.

Обычный журнал заданий работает в сокращённом режиме. Для диагностики уровень меняется централизованно в Semaphore: `Variable Groups → Infra Manager → INFRA_LOG_LEVEL` со значениями `normal`, `verbose` или `quiet`.

После первого развёртывания OpenBao инициализируется отдельным заданием `Initialize OpenBao 910`. Ключ снятия блокировки хранится только на PVE в `/root/.config/proxmox-bootstrap/openbao/unseal.key`; первоначальный корневой токен не сохраняется и сразу отзывается.

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
→ передать в 910 PVE CA, постоянный PVE token, root SSH и GitHub-доступ
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

Критичные постоянные данные:

```text
/var/lib/persistent/openbao/
/etc/infra-manager/ansible/
/mnt/pve-access/pve-host/
/var/lib/infra-manager/semaphore/
/var/lib/infra-manager/opentofu/state/
```

Рабочие PVE, Git и Semaphore secrets берутся из OpenBao и материализуются только во временный каталог `/run/infra-manager/secrets/`.

Постоянный OpenTofu state:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Он не хранится в Git и должен резервироваться.

## Доступ 910 к PVE

Постоянная идентичность API:

```text
root@pam!infra-manager
privsep=0
```

910 считается доверенным управляющим узлом домашнего PVE. API token используется OpenTofu и обычными API-операциями, а отдельный root SSH-ключ — только для действий, которые Proxmox запрещает выполнять API token.

Например профиль `container-host` остаётся единым логическим свойством:

```text
container-host
├── nesting=1  → OpenTofu / PVE API
└── keyctl=1   → общий host-only шаг через root SSH
```

Эта логика находится в общем `deploy-guest`, а не в специальном коде 910.

## GitHub Deploy Key

Первоначальный read-only ключ проекта хранится на PVE:

```text
/root/.config/proxmox-bootstrap/github_proxmox_repo_ed25519
```

Он пока используется как bootstrap-источник до появления OpenBao.

После инициализации рабочий закрытый ключ хранится в OpenBao KV v2 и материализуется для Semaphore в `/run/infra-manager/secrets/github_proxmox_repo_ed25519`.

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
- общие настройки `Infra Manager`;
- `OpenTofu Plan`;
- `Build Template 9000`;
- `Deploy Guest 410`;
- `Deploy Guest 910`;
- `Initialize OpenBao 910`.

## Первичный пароль Semaphore

Пароль создаётся один раз, сохраняется в OpenBao KV v2 и материализуется с правами `0600`:

```text
/run/infra-manager/secrets/initial-admin-password
```

После каждой успешной установки, в том числе повторной, установочный сценарий показывает адрес Semaphore, логин `admin` и сохранённый пароль. Общий процесс Ansible пароль не печатает, и в технический журнал он не записывается.

Получить его от root внутри 910 можно также явно:

```bash
cat /run/infra-manager/secrets/initial-admin-password
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
- 910 считается доверенным административным контуром физического PVE;
- AI и прикладные гости не получают root-доступ к PVE напрямую;
- `archive/` и устаревшая документация не являются источниками действующей конфигурации.
