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

В проекте создаются:

- GitHub project read-only;
- PVE API automation;
- Ansible managed guests;
- репозиторий `git@github.com:zsergeyru/proxmox.git`;
- Variable Group `OpenTofu PVE`;
- шаблон `OpenTofu Plan`.

Variable Group передаёт OpenTofu:

```text
TF_VAR_pve_endpoint
TF_VAR_pve_api_token
```

API token передаётся как секретная переменная окружения и не записывается в Git или аргументы командной строки.

`OpenTofu Plan` запускает отдельный `scripts/infra-deployer/opentofu-plan.sh`. Этот сценарий обновляет итоговый `guests.json` из текущей ревизии Git и выполняет только `tofu init` и `tofu plan`.

До отдельного решения о применении инфраструктуры шаблон `OpenTofu Apply` не создаётся. Контрактный тест запрещает появление `tofu apply` или `tofu destroy` в сценарии `OpenTofu Plan`.

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

Минимальный ACL-контракт для OpenTofu уже зафиксирован и применяется public bootstrap.

Изменяющие права ограничены пулом `managed`; `910` и шаблон `9000` не получают обычные права изменения.

До переноса архитектуры в `main` остаётся выполнить на реальном PVE явный тест полного жизненного цикла временного гостя:

```bash
infra-deployer-pve-lifecycle-test --apply
```

Тест использует технический CTID `9098`. Перед началом он обязан убедиться, что VMID свободен; существующий объект никогда не удаляется ради теста.

Последовательность:

```text
создать unprivileged LXC в managed
→ изменить RAM
→ запустить
→ проверить running
→ остановить
→ проверить stopped
→ удалить
→ подтвердить освобождение VMID
```

Статическая проверка и read-only проверка прав через API не заменяют этот тест, поэтому он не включён в обычный bootstrap или CI.

## 9. Связанные документы

- [`../200-pve/210-host-bootstrap.md`](../200-pve/210-host-bootstrap.md) — public bootstrap.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — права PVE API.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — конкретный гость `910`.
- [`830-recovery.md`](830-recovery.md) — восстановление.