# 311 — dev-services

**Тип:** LXC  
**Назначение:** DevOps, Ansible, CI/CD и сервисы разработки, отделённые от AI Control и обычных прикладных сервисов.

Правила административного SSH находятся в [`../../../docs/700-security/720-ssh-access.md`](../../../docs/700-security/720-ssh-access.md), а порядок создания и первоначальной подготовки гостя — в [`../../../docs/300-guests/330-deploy-guest.md`](../../../docs/300-guests/330-deploy-guest.md).

## Действующий desired state schema v9

`311-dev-services` — первый целевой сценарий полного Guest Bootstrap v1. Его рабочий `guest.yaml` уже явно содержит:

```yaml
management:
  - ssh_identity
  - project_repo_read

bootstrap:
  - git
  - docker
  - ansible
```

`ssh_identity` означает: после verified SSH deployer обеспечивает стандартную management SSH pair **внутри 311**, private оставляет там и регистрирует на PVE только `311.pub`.

`project_repo_read` независимо задаёт фиксированный read-only доступ к `zsergeyru/proxmox`.

Schema v9/resolver используют новый список Management и Bootstrap; состояние runtime handlers отдельно отражается в `29-implementation-status.md`.

Целевой deploy flow:

```text
create LXC 311 with management-authorized-keys
→ PVE tag management-ssh
→ first SSH host-key trust
→ verified root SSH deployer
→ own management identity
→ register 311.pub
→ sync-management-keys
→ Project Git READ desired state
→ git → docker → ansible
→ final acceptance
```

## Management SSH Ansible

311 не получает заранее известный специальный `ansible_ed25519` от PVE и не передаёт private key напрямую 301.

При:

```yaml
management:
  - ssh_identity
```

используется стандарт проекта:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Для 311 эта identity фактически является SSH identity Ansible:

```text
311 / Ansible
→ /etc/proxmox-guest/ssh/management_ed25519
→ SSH root@target
```

Private остаётся только в 311.

Public deployer регистрирует как:

```text
/var/lib/proxmox-deployer/public-keys/311.pub
```

Filename зависит только от VMID. Переименование LXC identity не меняет.

После регистрации `sync-management-keys` распространяет общий public-key set по Debian VM/LXC с tag `management-ssh`, включая 301. Сам 311 не подключается к PVE ради регистрации ключа.

При удалении 311 его `311.pub` удаляется из registry с последующим sync. Новый объект с VMID 311 получает новую management identity; старый key не переиспользуется.

## Project Git READ

311 не получает отдельный собственный GitHub Deploy Key для чтения.

Manifest:

```yaml
management:
  - project_repo_read
```

Используется общий read-only Project Git credential:

```text
repository: zsergeyru/proxmox
permission: read-only
master copy: PVE root-only
local key: /etc/proxmox-guest/credentials/github-proxmox-read
known_hosts: /etc/proxmox-guest/ssh/github-proxmox-known_hosts
SSH config: /etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf
```

Этот credential не является management SSH key Ansible и не добавляется в `authorized_keys`.

## Локальный public-key catalog

311 имеет PVE tag:

```text
management-ssh
```

и получает при синхронизации:

```text
/etc/proxmox-guest/public-keys/
├── deployer.pub
├── <VMID>.pub
└── management-authorized-keys
```

Это только public keys. Private keys других управляющих систем внутри 311 отсутствуют.

Канонический registry на PVE:

```text
/var/lib/proxmox-deployer/public-keys/
```

принадлежит `pvedeploy:pvedeploy`; распространение выполняет отдельный `sync-management-keys`.

## Планируемые сервисы

- Ansible — штатный механизм повторяемой настройки VM/LXC по SSH;
- Semaphore — web UI для ручных запусков, истории и журналов;
- Gitea или Gogs;
- Jenkins;
- другие DevOps-инструменты при необходимости.

Ansible и Semaphore относятся сюда, а не в `301-ai-control`.

Ansible устанавливается напрямую в ОС LXC `311-dev-services` через Bootstrap `ansible` и не зависит от Docker. Semaphore и другие прикладные DevOps-сервисы могут разворачиваться отдельно, в том числе в Docker. На target guests отдельный Ansible agent не устанавливается; Ansible подключается по SSH management identity 311.

## Взаимодействие с AI Control

```text
301-ai-control
  agent + Proximo
       │
       │ инициирует повторяемую настройку
       ▼
311-dev-services
  Ansible
       │ SSH по management identity 311
       ▼
managed VM/LXC
```

301 и 311 не синхронизируют keys напрямую. Оба получают единый public-key set от PVE через `sync-management-keys`.

Прямой SSH из AI Control допустим для диагностики и разовых действий. Штатная повторяемая конфигурация выполняется Ansible.

## Semaphore

Semaphore предназначен прежде всего для ручного web-управления:

```text
user
→ Semaphore
→ Ansible
→ SSH
→ guest
```

Он показывает запуски, результаты и логи. Остановка Semaphore не должна блокировать прямое автоматическое использование Ansible.

## Развёртывание `rootfs/`

Для `rootfs/` действует простая модель:

- проектные каталоги вроде `/opt/<service>/` можно синхронизировать целиком с удалением файлов, отсутствующих в Git;
- файлы общих системных каталогов (`/etc`, `/usr/local/bin`, systemd units) устанавливаются по конкретным путям;
- удаление отдельного системного файла — явная Ansible task;
- глобальный `rsync --delete rootfs/ /` запрещён;
- persistent data, Docker volumes, databases и secrets не являются `rootfs/`.

Полное решение: [`decisions/001-deployment-tooling.md`](decisions/001-deployment-tooling.md).

## Граница с другими гостями

Homarr и пользовательские приложения относятся к `321-app-services`.

Hermes, другие AI agents и MCP — к `301-ai-control`. Общие STT/TTS — к `331-ai-services`.

Compose files, config и локальный code `311-dev-services` после появления реальной установки хранятся под его `rootfs/`; фиктивные Compose-файлы заранее не создаются.