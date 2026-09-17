# 311 — dev-services

**Тип:** LXC  
**Назначение:** DevOps, Ansible, CI/CD и сервисы разработки, отделённые от AI Control и обычных прикладных сервисов.

Основной контракт административных SSH-ключей находится в [`../../docs/28-management-ssh-keys.md`](../../docs/28-management-ssh-keys.md).

## Guest Bootstrap v1

`311-dev-services` — первый целевой сценарий полного Guest Bootstrap v1. Его действующий `guest.yaml` явно запрашивает:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

После реализации принятых manifest-интерфейсов целевой `guest.yaml` 311 также должен содержать:

```yaml
management_key: true

access:
  project_repo_read: true
```

`management_key` означает: после появления проверенного SSH deployer создаёт стандартную management SSH-пару **внутри 311**, оставляет private key только там и регистрирует на PVE только открытую часть.

`project_repo_read` независимо выдаёт общий read-only Git credential для `zsergeyru/proxmox`.

Оба поля пока не реализованы в действующей schema v6, поэтому до изменения schemas/resolver/validator в рабочий manifest не добавляются.

После создания LXC `deploy-guest` должен получить проверенный `root SSH`, при необходимости создать/проверить management identity, зарегистрировать её `.pub`, материализовать Project Git READ credential, выполнить `base → git → docker → ansible_controller` и только после полной проверки завершить deploy.

## Management SSH Ansible

311 не получает специальный заранее известный `ansible_ed25519` от PVE и не передаёт ключ напрямую 301.

При `management_key: true` используется общий стандарт проекта:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Для 311 эта identity фактически является SSH-ключом Ansible:

```text
311 / Ansible
→ /etc/proxmox-guest/ssh/management_ed25519
→ SSH root@target
```

Закрытая часть остаётся только в 311.

Открытую часть deployer забирает после генерации и регистрирует как, например:

```text
/etc/proxmox-deployer/public-keys/311-dev-services.pub
```

После регистрации `sync-management-keys` распространяет обновлённый public-key registry по управляемым Debian VM/LXC, включая 301. Сам 311 не подключается ни к PVE, ни к 301 ради регистрации или распространения ключа.

## Доступ к проектному Git

311 не получает собственного отдельного GitHub Deploy Key для чтения.

Используется общий Project Git READ credential:

```text
GitHub: zsergeyru/proxmox
permission: read-only
master copy: PVE root-only
local copy: /etc/proxmox-guest/credentials/github-proxmox-read
```

Этот credential не является management SSH-ключом Ansible и не добавляется в `authorized_keys`.

## Локальная копия public-key registry

Как и другие управляемые Debian-гости, 311 получает при синхронизации:

```text
/etc/proxmox-guest/public-keys/
└── management-authorized-keys
```

Это только открытые ключи. Закрытых ключей других управляющих систем внутри 311 нет.

Основной источник каталога находится на PVE:

```text
/etc/proxmox-deployer/public-keys/
```

а распространение выполняет отдельная команда `sync-management-keys`.

## Планируемые сервисы

- Ansible — штатный механизм повторяемой настройки VM/LXC через SSH;
- Semaphore — веб-интерфейс к Ansible для ручного запуска, истории и журналов;
- Gitea или Gogs;
- Jenkins;
- другие инструменты разработки, сборки и CI/CD при необходимости.

Ansible и Semaphore относятся именно сюда, а не в `301-ai-control`: это инфраструктурные и DevOps-инструменты, а не AI-компоненты.

Целевой вариант — запуск Ansible и Semaphore в Docker внутри `311-dev-services`. На управляемые гости отдельный агент Ansible не устанавливается; Ansible подключается к ним по SSH management key 311.

## Взаимодействие с AI Control

```text
301-ai-control
  агент + Proximo
       │
       │ инициирует повторяемую настройку
       ▼
311-dev-services
  Ansible
       │ SSH по собственной management identity
       ▼
управляемые VM/LXC
```

301 и 311 не синхронизируют ключи друг с другом напрямую. Оба получают единый набор открытых management keys от PVE через `sync-management-keys`.

Прямой SSH из AI Control остаётся допустимым для диагностики и разовых действий. Штатная повторяемая настройка выполняется Ansible.

## Semaphore

Semaphore предназначен прежде всего для ручного управления через веб-интерфейс:

```text
пользователь
→ Semaphore
→ Ansible
→ SSH
→ нужная гостевая система
```

Semaphore показывает запуски, результаты и журналы. Он не является обязательным промежуточным слоем между AI и Ansible: остановка Semaphore не должна блокировать автоматическое управление инфраструктурой.

## Развёртывание `rootfs/`

Для `rootfs/` используется максимально простая модель:

- выделенные каталоги, полностью принадлежащие проекту, например `/opt/<service>/`, можно синхронизировать целиком с удалением файлов, которых больше нет в Git;
- файлы в общих системных каталогах (`/etc`, `/usr/local/bin`, systemd units и т. п.) устанавливаются только по конкретным путям;
- удаление отдельного системного файла выполняется явной задачей Ansible;
- глобальный `rsync --delete rootfs/ /` запрещён;
- постоянные данные, Docker volumes, базы данных и секреты не являются `rootfs/`.

Полное принятое решение: [`decisions/001-deployment-tooling.md`](decisions/001-deployment-tooling.md).

## Граница с другими гостевыми системами

Homarr и обычные пользовательские приложения сюда не относятся; они размещаются в `321-app-services`.

Hermes, другие AI-агенты и MCP остаются в `301-ai-control`. Общие STT/TTS — в `331-ai-services`.

Compose-файлы, конфигурация и локальный код `311-dev-services` после появления реальной установки хранятся под его `rootfs/`. Фиктивные Compose-файлы заранее не создаются.