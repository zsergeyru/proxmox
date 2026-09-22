# 910 infra-deployer

`910 infra-deployer` — специальный постоянный LXC, из которого выполняется штатное развёртывание инфраструктуры проекта.

Публичный `bootstrap-pve.sh` создаёт только сам виртуальный объект 910, минимальный доступ к PVE и доступ к закрытому Git. Всё внутреннее программное наполнение 910 принадлежит закрытому проекту.

## Назначение

Внутри 910 работают:

- Semaphore Server;
- Semaphore Runner;
- OpenTofu;
- Ansible;
- Packer;
- Git и вспомогательные средства.

```text
PVE
└─ public bootstrap
   └─ 910 infra-deployer
      └─ private setup
         ├─ Semaphore
         ├─ Runner
         ├─ OpenTofu
         ├─ Ansible
         └─ Packer
```

## Особенность гостя

`910` является исключением из обычного контура гостей:

- его виртуальным объектом владеет public bootstrap;
- OpenTofu не создаёт и не изменяет 910;
- 910 не входит в pool `managed`;
- управление PVE выполняется через HTTPS API;
- постоянный root SSH с 910 на PVE не используется;
- Docker, Semaphore и инфраструктурные средства устанавливаются уже внутри 910.

## Состав первой версии

Используются:

```text
Semaphore Server v2.18.30
SQLite
Semaphore Runner v2.18.30
OpenTofu 1.12.6
Packer 1.15.4
Ansible
proxmoxer
```

Runner собирается поверх официального образа Semaphore. В нём включена проверка SSH host keys.

## Постоянные данные

Основные области:

```text
/etc/infra-deployer/
/var/lib/infra-deployer/
/opt/infra-deployer/
```

К критичным данным относятся:

- база Semaphore;
- ключ шифрования Semaphore;
- OpenTofu state;
- PVE API credential;
- GitHub Deploy Key;
- другие постоянные секреты, которые появятся у реально используемых функций.

Git checkout, кэш заданий, пакеты и образы контейнеров считаются воспроизводимыми.

## PVE API

Используется:

```text
root@pam!infra-deployer
```

Это ограниченный token с `privsep=1`.

Отдельный пользователь `infra-deployer@pve` и собственная роль `InfraManagedGuest` больше не используются.

Изменяющие права на обычные VM/LXC ограничены pool `managed`. Сам 910 находится вне него.

Постоянный credential хранится:

```text
/etc/infra-deployer/secrets/pve-api.env
```

OpenTofu получает его через секрет Variable Group `OpenTofu PVE`. Отдельная копия PVE credential в Semaphore Key Store не создаётся.

## GitHub

GitHub Deploy Key создаётся внутри 910 и используется только для чтения:

```text
git@github.com:zsergeyru/proxmox.git
```

Private key не хранится на PVE.

При первом запуске public bootstrap показывает открытый ключ. Его добавление в GitHub — единственное обязательное ручное действие первоначальной установки.

## Semaphore

Автоматически создаются только объекты, которые реально используются первой версией:

- `GitHub project read-only`;
- репозиторий `proxmox`;
- Variable Group `OpenTofu PVE`;
- `OpenTofu Plan`.

PVE API credential в Key Store и Ansible SSH credential заранее не создаются.

Ansible credential будет добавлен вместе с первой реальной Ansible-задачей, когда станет понятен её точный доступ.

## Локальные команды

После настройки доступны:

```bash
infra-deployer-status
infra-deployer-status --full
infra-deployer-pve-access-check
infra-deployer-pve-lifecycle-test --apply
```

`infra-deployer-status` проверяет сам 910, Semaphore, Runner, инструменты и базовую авторизацию PVE API.

`--full` дополнительно запускает проверку фактических PVE-прав.

Тест жизненного цикла создаёт временный LXC 9098 в `managed`, изменяет его, запускает, останавливает и удаляет.

## Первый запуск

На PVE:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/infra-iac-redesign/bootstrap-pve.sh | bash
```

Если GitHub Deploy Key ещё не зарегистрирован, bootstrap показывает public key и ждёт его добавления в GitHub.

После этого весь оставшийся процесс выполняется автоматически.

## OpenTofu state

Первая версия хранит состояние локально:

```text
/var/lib/infra-deployer/opentofu/state/proxmox.tfstate
```

State не хранится в Git и обязательно резервируется.

Потеря state не должна приводить к автоматическому `apply` с новым пустым состоянием поверх существующей инфраструктуры.

## Обновление

При новой ревизии закрытого проекта public bootstrap повторно запускает внутренний `setup.sh`.

Настройка должна быть повторяемой:

- постоянные данные сохраняются;
- существующие секреты повторно используются;
- устаревшие элементы прошлой схемы удаляются;
- текущие объекты Semaphore синхронизируются;
- результат проверяется.

## Восстановление

Если потерян API secret, используется явный режим:

```bash
bootstrap-pve.sh --recover
```

Восстановление постоянных данных 910 описывается отдельно. Пустое OpenTofu state поверх существующей инфраструктуры автоматически не применяется.

## Границы

910 не используется для Home Assistant, MQTT, Zigbee2MQTT, ESPHome, Frigate, пользовательских приложений или общего файлового хранилища.

## Связанные документы

- [`../../docs/200-pve/210-host-bootstrap.md`](../../docs/200-pve/210-host-bootstrap.md) — первоначальная подготовка.
- [`../../docs/700-security/710-pve-access.md`](../../docs/700-security/710-pve-access.md) — PVE API-доступ.
- [`../../docs/800-operations/810-deployment.md`](../../docs/800-operations/810-deployment.md) — процесс развёртывания.
- [`decisions.md`](decisions.md) — устойчивые решения 910.
