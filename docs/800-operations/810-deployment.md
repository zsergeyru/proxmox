# Развёртывание инфраструктуры

**Тип:** эксплуатация  
**Статус:** проектируется  
**Назначение:** описать штатный путь от PVE bootstrap до выполнения инфраструктурных заданий из `910 infra-deployer`.

## 1. Общая схема

```text
PVE
→ public bootstrap
→ 910 infra-deployer
→ Semaphore
→ Runner
→ OpenTofu / Ansible / Packer
→ обычные VM/LXC
```

Сам PVE не содержит постоянную закрытую Git-копию проекта и не является местом выполнения обычных инфраструктурных заданий.

## 2. Создание 910

`910 infra-deployer` создаётся публичным `zsergeyru/proxmox-bootstrap`.

Bootstrap отвечает за объект LXC `910`, его ресурсы и сеть, `protection=1`, PVE API identity, PVE CA, первоначальный GitHub Deploy Key и передачу управления закрытому `scripts/infra-deployer/setup.sh`.

Сам `910` не входит в собственное OpenTofu state.

## 3. Внутренняя подготовка 910

`scripts/infra-deployer/setup.sh` внутри `910`:

```text
проверить Debian 13
→ установить Docker Engine
→ создать постоянные каталоги
→ сохранить bootstrap credentials
→ запустить Semaphore Server
→ запустить Semaphore Runner
→ создать проект Semaphore
→ создать Key Store
→ зарегистрировать private Git
→ установить infra-deployer-status
→ проверить результат
```

Повторный запуск должен повторно использовать существующие постоянные данные и секреты.

## 4. Semaphore

Первая версия использует один проект `Proxmox Infrastructure`.

В проекте создаются как минимум GitHub project read-only, PVE API automation, Ansible managed guests и репозиторий `git@github.com:zsergeyru/proxmox.git`.

Для управления самим Semaphore создаётся отдельный API token, который хранится внутри `910` и позволяет менять пароль администратора без поломки автоматической настройки.

## 5. Runner

Runner является единственным штатным исполнителем инфраструктурных заданий.

Он содержит Ansible, OpenTofu, Packer, Python, `proxmoxer`, Git и OpenSSH client.

Одновременно разрешается одно инфраструктурное задание, изменяющее основное OpenTofu state.

SSH host key checking включён и не отключается ради автоматизации.

## 6. OpenTofu state

Основной каталог:

```text
/var/lib/infra-deployer/opentofu/state/
```

State не хранится в Git, резервируется, не создаётся заново поверх существующей инфраструктуры после потери и не изменяется несколькими заданиями одновременно.

## 7. Проверка

Внутри `910`:

```bash
infra-deployer-status
```

Проверяются постоянные секреты и CA, Semaphore Server, Semaphore Runner, Semaphore API, OpenTofu, Packer, Ansible, `proxmoxer` и каталог OpenTofu state.

Публичный bootstrap использует эту же проверку при повторном `--check`.

## 8. Граница текущего этапа

Наличие готового `910` ещё не означает, что разрешены изменения обычных VM/LXC.

До завершения проверки минимальных прав OpenTofu PVE token получает только безопасный доступ чтения. Права изменения добавляются отдельным следующим этапом.

## 9. Связанные документы

- [`../200-pve/210-host-bootstrap.md`](../200-pve/210-host-bootstrap.md) — public bootstrap.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — права PVE API.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — конкретный гость `910`.
- [`830-recovery.md`](830-recovery.md) — восстановление.