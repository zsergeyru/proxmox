# Домашняя инфраструктура Proxmox

Закрытый репозиторий конфигурации, кода и документации домашней инфраструктуры на Proxmox VE.

Здесь хранится требуемое состояние инфраструктуры, документация, сценарии развёртывания и настройка постоянного управляющего контейнера `910 infra-manager`.

## Быстрый старт

Обычная установка или повторное применение без параметров выполняется на физическом PVE от `root` сразу из публичного репозитория:

~~~bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/infra-iac-redesign/bootstrap-pve.sh | bash
~~~

Если нужны параметры, сценарий сначала скачивается:

~~~bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/infra-iac-redesign/bootstrap-pve.sh -o bootstrap-pve.sh
chmod +x bootstrap-pve.sh
~~~

Справка:

~~~bash
./bootstrap-pve.sh --help
~~~

Публичный репозиторий:

~~~text
https://github.com/zsergeyru/proxmox-bootstrap
~~~

На первом запуске bootstrap создаёт постоянный GitHub Deploy Key на PVE и показывает открытый ключ. Его нужно один раз добавить в `zsergeyru/proxmox` как read-only Deploy Key.

После этого повторные установки и пересоздания 910 используют тот же ключ.

## Общая архитектура

~~~text
GitHub public: zsergeyru/proxmox-bootstrap
        │
        │ bootstrap-pve.sh
        ▼
физический PVE
        │
        ├── постоянный GitHub Deploy Key
        ├── LXC 910 infra-manager
        └── ограниченный PVE API-доступ для 910
                         │
                         ▼
                 LXC 910 infra-manager
                         │
                         ├── закрытый repo zsergeyru/proxmox
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
                                  │
                                  └── pool managed
~~~

Физический PVE должен оставаться максимально чистым. Docker, Semaphore, OpenTofu, Ansible, Packer и прикладные сервисы на хост не устанавливаются.

## Что делает публичный bootstrap

`bootstrap-pve.sh` выполняется только на PVE.

~~~text
проверить root и PVE
→ получить блокировку
→ проверить vmbr0, local и local-lvm
→ создать или проверить LXC 910
→ запустить 910 и дождаться сети
→ создать или использовать постоянный GitHub Deploy Key
→ минимально подготовить Debian внутри 910
→ передать GitHub Deploy Key внутрь 910
→ проверить read-only доступ к закрытому проекту
→ получить или обновить закрытый проект
→ выполнить scripts/infra-manager/pve-bootstrap-access.sh на PVE
→ выполнить scripts/infra-manager/setup.sh внутри 910
→ проверить итоговое состояние
~~~

В публичном bootstrap не хранится конкретная политика PVE ACL и не описывается внутренняя настройка Semaphore/OpenTofu/Ansible/Packer.

## LXC 910 infra-manager

~~~text
CTID:        910
hostname:    infra-manager
OS:          Debian 13
unprivileged yes
CPU:         2
RAM:         2048 MiB
swap:        512 MiB
root disk:   32 GiB
storage:     local-lvm
bridge:      vmbr0
onboot:      yes
protection:  yes
features:    nesting=1,keyctl=1
tags:        infra-manager;proxmox-bootstrap
~~~

910 является специальным управляющим контейнером: его создаёт публичный bootstrap, OpenTofu не управляет самим 910, 910 не входит в `managed`, постоянный root SSH с 910 на PVE не используется.

Подробнее: [`guests/910-infra-manager/README.md`](guests/910-infra-manager/README.md).

## GitHub Deploy Key

Постоянный ключ хранится на PVE:

~~~text
/root/.config/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
└── github_proxmox_repo_ed25519.pub
~~~

Права: каталог `0700`, private key `0600`, public key `0644`.

При создании или повторной настройке 910 bootstrap копирует этот ключ внутрь контейнера для чтения закрытого репозитория. Удаление и повторное создание 910 не требует нового Deploy Key.

## Debian template

Если подходящий Debian 13 LXC template уже существует в `local:vztmpl`, bootstrap использует его и не считает своим.

Если template отсутствует:

~~~text
скачать Debian 13 template
→ отметить его как временный
→ создать 910
→ сразу удалить скачанный template
~~~

После успешной установки скачанный bootstrap template на PVE не остаётся. Если установка прервалась, он удаляется при `--remove` или `--purge`. Заранее существовавший template автоматически не удаляется.

## Закрытый проект внутри 910

Рабочая копия:

~~~text
/var/lib/infra-manager/bootstrap-repo
~~~

Источник:

~~~text
git@github.com:zsergeyru/proxmox.git
~~~

По умолчанию используется ветка `infra-iac-redesign`. При повторном bootstrap рабочая копия обновляется до текущего состояния этой ветки.

Основные сценарии:

~~~text
scripts/infra-manager/pve-bootstrap-access.sh
scripts/infra-manager/setup.sh
scripts/infra-manager/semaphore-project.sh
scripts/infra-manager/opentofu-plan.sh
scripts/infra-manager/render-opentofu-input.py
scripts/infra-manager/status.sh
scripts/infra-manager/check-pve-access.sh
scripts/infra-manager/test-pve-lifecycle.sh
~~~

## Доступ 910 к PVE

Используется API token:

~~~text
root@pam!infra-manager
privsep=1
~~~

Политика доступа хранится только в `scripts/infra-manager/pve-bootstrap-access.sh`.

| Путь | Роль | Назначение |
|---|---|---|
| `/` | `PVEAuditor` | чтение состояния PVE |
| `/vms` | `PVEVMAdmin` | управление всеми VM/LXC |
| `/pool/managed` | `PVEVMAdmin`, `PVEPoolUser` | назначение гостей и чтение pool |
| `/storage/local-lvm` | `PVEDatastoreUser` | диски VM/LXC |
| `/storage/local` | `PVEDatastoreAdmin` | установочные ISO для Packer |
| `/sdn/zones/localnetwork/vmbr0` | `PVESDNUser` | использование основной сети |

910 находится вне `managed`.

Подробнее: [`docs/700-security/710-pve-access.md`](docs/700-security/710-pve-access.md).

## Что работает внутри 910

~~~text
infra-runtime v1
├── Semaphore v2.18.30 + SQLite
├── OpenTofu 1.12.6
├── Packer 1.15.4
├── Ansible
└── proxmoxer
~~~

Основные постоянные области:

~~~text
/etc/infra-manager/
/var/lib/infra-manager/
/opt/infra-manager/
~~~

OpenTofu state хранится локально:

~~~text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
~~~

State не хранится в Git и должен резервироваться.

## Semaphore

Bootstrap автоматически подготавливает используемые объекты Semaphore:

~~~text
Project
Git repository proxmox
OpenTofu PVE Variable Group
OpenTofu Plan
~~~

Ansible credential заранее не создаётся. Он добавляется только вместе с первой реальной Ansible-задачей.

## Служебные команды 910

~~~bash
infra-manager-status
infra-manager-status --full
infra-manager-pve-access-check
infra-manager-pve-lifecycle-test --apply
~~~

## Технический лог bootstrap

~~~text
/var/log/infra-manager/bootstrap.log
~~~

На экран выводятся основные этапы, успешные проверки, предупреждения и ошибки; подробный служебный вывод хранится в этом файле.

## Команды публичного bootstrap

Без параметров можно запускать сразу из GitHub:

~~~bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/infra-iac-redesign/bootstrap-pve.sh | bash
~~~

Для остальных режимов используется скачанный `bootstrap-pve.sh`.

Скачать:

~~~bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/infra-iac-redesign/bootstrap-pve.sh -o bootstrap-pve.sh
chmod +x bootstrap-pve.sh
~~~

Справка:

~~~bash
./bootstrap-pve.sh --help
~~~

Только проверка:

~~~bash
./bootstrap-pve.sh --check
~~~

Восстановление потерянного PVE API token:

~~~bash
./bootstrap-pve.sh --recover
~~~

Мягкое удаление:

~~~bash
./bootstrap-pve.sh --remove
~~~

Полное удаление:

~~~bash
./bootstrap-pve.sh --purge
~~~

Статический адрес 910:

~~~bash
./bootstrap-pve.sh \
  --ip 192.168.1.90/24 \
  --gateway 192.168.1.1
~~~

Другая ветка закрытого проекта:

~~~bash
./bootstrap-pve.sh --project-branch NAME
~~~

## Мягкое и полное удаление

`--remove` удаляет LXC 910, API token, ACL, пустой `managed` и временный Debian template, если он остался. Постоянный GitHub Deploy Key на PVE сохраняется.

`--purge` делает то же самое и дополнительно удаляет `/root/.config/proxmox-bootstrap/` вместе с GitHub Deploy Key.

Не удаляются автоматически VM 100 HAOS, хранилище `backup`, чужой объект с VMID 910, непустой `managed`, заранее существовавший Debian template и другие VM/LXC.

## Структура репозитория

~~~text
proxmox/
├── docs/        документация
├── guests/      описание VM/LXC
├── host/pve/    состояние физического PVE
├── scripts/     сценарии управления и проверки
├── templates/   шаблоны
├── ansible/     повторяемая настройка Linux-гостей
├── schemas/     схемы guest/defaults/effective
└── archive/     исторические материалы
~~~

## Документация

- [`docs/100-architecture/`](docs/100-architecture/) — архитектура;
- [`docs/200-pve/`](docs/200-pve/) — PVE и первоначальная подготовка;
- [`docs/300-guests/`](docs/300-guests/) — гости и их требуемое состояние;
- [`docs/400-network/`](docs/400-network/) — сеть, DNS, маршрутизация и VPN;
- [`docs/500-ai/`](docs/500-ai/) — AI-управление;
- [`docs/600-storage/`](docs/600-storage/) — хранилища и резервные копии;
- [`docs/700-security/`](docs/700-security/) — доступ и безопасность;
- [`docs/800-operations/`](docs/800-operations/) — эксплуатация и восстановление.

С bootstrap особенно связаны:

- [`docs/200-pve/210-host-bootstrap.md`](docs/200-pve/210-host-bootstrap.md);
- [`docs/700-security/710-pve-access.md`](docs/700-security/710-pve-access.md);
- [`docs/800-operations/810-deployment.md`](docs/800-operations/810-deployment.md);
- [`guests/910-infra-manager/README.md`](guests/910-infra-manager/README.md).

## Общие правила

- Git хранит воспроизводимую конфигурацию и документацию, но не рабочие секреты.
- Закрытые ключи, пароли и токены в Git не добавляются.
- PVE остаётся максимально чистым гипервизором.
- Обычные управляемые VM/LXC входят в `managed`.
- 910 является управляющим исключением и находится вне `managed`.
- Новые права PVE не выдаются заранее: они добавляются только под реально используемую операцию.
- `archive/` и `docs/legacy/` не являются источниками действующей конфигурации.
