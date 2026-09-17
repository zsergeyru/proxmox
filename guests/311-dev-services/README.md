# 311 — dev-services

**Тип:** LXC  
**Назначение:** DevOps, развёртывание, CI/CD и сервисы разработки, отделённые от AI Control и обычных прикладных сервисов.

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

После реализации принятого manifest-интерфейса доступа к проектному Git целевой `guest.yaml` 311 также должен содержать:

```yaml
access:
  project_repo_read: true
```

На момент фиксации этого решения `access.project_repo_read` ещё не реализован в schema v6, поэтому поле не добавляется в действующий manifest до изменения schemas/resolver/validator.

После создания LXC `deploy-guest` должен получить проверенный `root SSH`, при `project_repo_read` материализовать общий read-only Git credential, затем последовательно проверить/применить `base → git → docker → ansible_controller` и только после успешной финальной проверки снять `deploy-incomplete`.

Bootstrap подготавливает сам управляющий узел Ansible, но **не** устанавливает Semaphore, Gitea/Gogs, Jenkins и другие прикладные сервисы. Они остаются зоной последующего Ansible provisioning.

Полный контракт: [`../../docs/31-deploy-guest.md`](../../docs/31-deploy-guest.md) и [`../../docs/33-guest-bootstrap-and-provisioning.md`](../../docs/33-guest-bootstrap-and-provisioning.md).

## Доступ к проектному Git

311 не получает собственный отдельный GitHub Deploy Key для чтения.

Используется общий project read-only credential:

```text
GitHub: zsergeyru/proxmox
permission: read-only
master copy: PVE root-only
local copy: /etc/proxmox-guest/credentials/github-proxmox-read
```

Он выдаётся только при явном:

```yaml
access:
  project_repo_read: true
```

Поле не содержит имя секрета или путь к нему. Связка с конкретным read-only Deploy Key жёстко определена deploy-контуром.

Общий Git read-key не является ключом Ansible для SSH в управляемые гости и не даёт write-доступ к GitHub.

## Планируемые сервисы

- Ansible — штатный механизм повторяемой настройки внутри VM/LXC через SSH;
- Semaphore — веб-интерфейс к Ansible для ручного запуска, истории и журналов;
- Gitea или Gogs;
- Jenkins;
- другие инструменты разработки, сборки и CI/CD при необходимости.

Ansible и Semaphore относятся именно сюда, а не в `301-ai-control`: они являются инфраструктурными и DevOps-инструментами, а не AI-компонентами.

Целевой вариант — запуск Ansible и Semaphore в Docker внутри `311-dev-services`. На управляемые гостевые системы отдельный агент Ansible не устанавливается; Ansible подключается к ним по SSH.

## Взаимодействие с AI Control

```text
301-ai-control
  Hermes
  Proxmox MCP
       │
       │ инициирует повторяемую настройку
       ▼
311-dev-services
  Ansible
       │ SSH
       ▼
управляемые VM/LXC
```

Hermes отвечает за принятие решения, изменение или подготовку конфигурации в Git и запуск операции. Ansible повторяемо применяет нужную конфигурацию внутри гостевой ОС.

301 и 311 могут использовать одну и ту же **read-only** Git credential для получения актуального `zsergeyru/proxmox`, но их административные SSH-ключи остаются независимыми.

Прямой SSH из AI Control остаётся допустимым для диагностики, разовых действий и аварийных случаев. Штатная первичная подготовка `311` теперь является частью `deploy-guest` через Guest Bootstrap v1.

## Semaphore

Semaphore предназначен прежде всего для ручного управления через веб-интерфейс:

```text
пользователь
→ Semaphore
→ Ansible
→ SSH
→ нужная гостевая система
```

Semaphore показывает запуски, результат и журналы. Он не является обязательным промежуточным слоем между Hermes и Ansible: остановка Semaphore не должна блокировать автоматическое управление инфраструктурой.

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

Hermes, Agent Zero и MCP остаются в `301-ai-control`. Общие STT/TTS — в `331-ai-services`.

Compose-файлы, конфигурация и локальный код `311-dev-services` после появления реальной установки хранятся под его `rootfs/`. Фиктивные Compose-файлы заранее не создаются.
