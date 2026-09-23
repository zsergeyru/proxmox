# Развёртывание инфраструктуры

**Тип:** эксплуатация  
**Статус:** проектируется  
**Назначение:** описать штатный путь от чистого PVE до выполнения инфраструктурных заданий из `910 infra-deployer`.

## 1. Общая схема

```text
PVE
→ минимальный public bootstrap
→ 910 infra-deployer
→ закрытый проект внутри 910
→ Semaphore
→ Runner
→ OpenTofu / Ansible / Packer
→ обычные VM/LXC
```

PVE не содержит постоянную закрытую копию проекта и не выполняет обычные инфраструктурные задания.

## 2. Роль публичного bootstrap

Публичный сценарий отвечает только за:

- создание или проверку LXC 910;
- запуск 910;
- передачу PVE CA;
- создание или повторное использование постоянного read-only GitHub Deploy Key на PVE и передачу его копии внутрь 910;
- получение закрытого проекта;
- запуск из закрытого проекта временного PVE access helper;
- запуск внутреннего `setup.sh`;
- проверку результата.

Единственное ручное действие при первом запуске — добавить показанный GitHub Deploy Key в GitHub как read-only.

Публичный сценарий не содержит логику Docker, Semaphore, OpenTofu, Ansible, Packer и не содержит матрицу PVE ACL. Политика доступа хранится в закрытом проекте.

## 3. Внутренняя подготовка 910

После получения закрытого проекта:

```text
scripts/infra-deployer/setup.sh
```

выполняет:

```text
проверить Debian 13
→ подготовить постоянные каталоги
→ сохранить PVE API credential
→ установить Docker
→ запустить Semaphore Server и Runner
→ подготовить OpenTofu input и state
→ настроить проект Semaphore
→ установить локальные команды проверки
→ проверить результат
```

Повторный запуск использует существующие постоянные данные и приводит 910 к текущей ревизии проекта.

## 4. Semaphore

Первая версия использует один проект:

```text
Proxmox Infrastructure
```

В нём автоматически создаются только используемые сейчас объекты:

- SSH credential `GitHub project read-only`;
- Git repository `git@github.com:zsergeyru/proxmox.git`;
- Variable Group `OpenTofu PVE`;
- шаблон `OpenTofu Plan`;
- шаблон `Build Template 9000`.

PVE API token не дублируется в Key Store. Он передаётся OpenTofu как секрет Variable Group:

```text
TF_VAR_pve_endpoint
TF_VAR_pve_api_token
```

Ansible SSH credential не создаётся заранее. Он появится только вместе с первой реальной Ansible-задачей, которой такой доступ понадобится.

Отдельный API token Semaphore используется только внутренней автоматизацией настройки самого Semaphore.

## 5. Runner

Runner содержит:

- OpenTofu;
- Ansible;
- Packer;
- Python и `proxmoxer`;
- Git;
- OpenSSH client.

Он является штатным исполнителем инфраструктурных заданий.

Для сборки базового шаблона используется простой путь:

```text
Semaphore: Build Template 9000
→ scripts/infra-deployer/build-template.sh 9000
→ Packer
→ tpl-debian13 (9000, Template-Version 8)
→ короткая проверка Full Clone через 9099
```

Packer использует установочный Debian ISO. Файл `preseed.cfg` временно отдаётся установщику самим Runner по HTTP; отдельный постоянный сервис для этого не создаётся.

Одновременно допускается только одно задание, изменяющее основное состояние OpenTofu.

Проверка SSH host key остаётся включённой.

## 6. OpenTofu

OpenTofu использует HTTPS API PVE и локальное состояние:

```text
/var/lib/infra-deployer/opentofu/state/proxmox.tfstate
```

Состояние:

- не хранится в Git;
- входит в резервное копирование 910;
- не должно создаваться пустым поверх уже существующей управляемой инфраструктуры после потери;
- не изменяется параллельно несколькими заданиями.

`910` не входит в собственное состояние OpenTofu.

## 7. Проверка

Основная команда внутри 910:

```bash
infra-deployer-status
```

Расширенная проверка PVE API:

```bash
infra-deployer-status --full
```

Реальный тест жизненного цикла:

```bash
infra-deployer-pve-lifecycle-test --apply
```

Ранее тест уже успешно создал, изменил, запустил, остановил и удалил временный LXC 9098 на реальном PVE. После перехода на новую упрощённую схему token/ACL он выполняется повторно один раз как приёмочная проверка.

## 8. Обновление 910

При повторном запуске public bootstrap обновляет выбранную ветку закрытого проекта и повторно запускает идемпотентный `setup.sh`.

```text
обновить checkout
→ подготовить PVE access
→ повторно применить setup.sh
→ проверить infra-deployer-status
```

Постоянные секреты при обычном обновлении не перевыпускаются.

## 9. Граница первой версии

На текущем этапе автоматизируются только уже используемые возможности.

Не создаются заранее:

- отдельные PVE-пользователи для будущих инструментов;
- отдельный PVE credential в Semaphore Key Store;
- Ansible SSH credential без реальной Ansible-задачи;
- дополнительные роли PVE без подтверждённой необходимости.

## 10. Связанные документы

- [`../200-pve/210-host-bootstrap.md`](../200-pve/210-host-bootstrap.md) — первоначальная подготовка.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — доступ 910 к PVE.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — паспорт 910.
- [`830-recovery.md`](830-recovery.md) — восстановление.
