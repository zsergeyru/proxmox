# Развёртывание инфраструктуры

**Тип:** эксплуатация  
**Статус:** проектируется  
**Назначение:** описать штатный путь от чистого PVE до выполнения инфраструктурных заданий из `910 infra-manager`.

## 1. Общая схема

~~~text
PVE
→ минимальный public bootstrap
→ временный 990 bootstrap-runner
→ отдельный bootstrap-state
→ общий OpenTofu + Ansible
→ 910 infra-manager
→ Semaphore
→ основной OpenTofu / Ansible / Packer
→ остальные VM/LXC
~~~

PVE не содержит постоянную закрытую копию проекта и не выполняет обычные инфраструктурные задания.

## 2. Роль public bootstrap

Public bootstrap отвечает только за минимальную подготовку 990:

- создать или безопасно повторно использовать временный LXC 990;
- дать 990 read-only доступ к закрытому Git;
- выполнить закрытый helper временного PVE API-доступа;
- передать PVE CA и сохранённый bootstrap-state 910;
- запустить закрытый runner;
- после успеха сохранить state, отозвать временный token и удалить 990.

Параметры 910 и матрица его постоянных прав не дублируются в публичном сценарии.

## 3. Развёртывание 910

Внутри 990 используется тот же `infra-runtime`, общий OpenTofu и общий Ansible.

~~~text
scripts/bootstrap-runner/run.sh
→ build infra-runtime
→ deploy-910.py
→ run_deploy_guest(..., 910, exclusive=True)
~~~

Отдельный state содержит только VMID 910 и сохраняется на PVE в:

~~~text
/var/lib/proxmox-bootstrap/state/910.tfstate
~~~

После создания виртуального объекта повторяемое внутреннее состояние 910 применяется средствами закрытого проекта. Основной state самого 910 всегда исключает VMID 910.

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

Локальная аутентификация Semaphore остаётся включённой. Первичный пароль `admin` хранится в `/etc/infra-manager/secrets/initial-admin-password` и один раз показывается в терминале, пока ещё не была создана отметка о его показе. В bootstrap-журнал пароль не записывается.

## 5. Исполнение заданий

Отдельный remote Runner не используется. Для одного домашнего `910` Semaphore выполняет задания локально и содержит:

- OpenTofu;
- Ansible;
- Packer;
- Python и `proxmoxer`;
- Git;
- OpenSSH client.

Сам контейнер Semaphore является штатным исполнителем инфраструктурных заданий.

Для сборки базового шаблона используется простой путь:

```text
Semaphore: Build Template 9000
→ Python: scripts/infra-manager/jobs/build-template.py 9000
→ infra_manager.template
→ Packer
→ tpl-debian13 (9000, Template-Version 8)
→ короткая проверка Full Clone через 9099
```

Packer использует установочный Debian ISO. Файл `preseed.cfg` временно отдаётся установщику самим Semaphore по HTTP; контейнер работает в сети 910 напрямую, поэтому отдельный Runner или постоянный веб-сервис не нужен.

Одновременно допускается только одно задание, изменяющее основное состояние OpenTofu.

Проверка SSH host key остаётся включённой.

## 6. OpenTofu

OpenTofu использует HTTPS API PVE и локальное состояние:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
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
infra-manager-status
```

Расширенная проверка PVE API:

```bash
infra-manager-status --full
```

Реальный тест жизненного цикла:

```bash
/usr/local/sbin/infra-manager-pve-lifecycle-test --apply
```

Ранее тест уже успешно создал, изменил, запустил, остановил и удалил временный LXC 9098 на реальном PVE. После перехода на новую упрощённую схему token/ACL он выполняется повторно один раз как приёмочная проверка.

## 8. Обновление 910

Изменения виртуального объекта 910 выполняются тем же bootstrap-контуром, что его создание:

~~~text
public bootstrap
→ временный 990
→ существующий bootstrap-state 910
→ run.sh deploy
→ при необходимости постоянный PVE helper 910
→ run.sh configure
→ проверить 910
→ сохранить state
→ удалить 990
~~~

Если меняется только воспроизводимое внутреннее состояние уже работающего 910, его идемпотентный setup может применяться без пересоздания LXC. Однако параметры самого объекта Proxmox никогда не меняются OpenTofu из основного state самого 910.

Постоянные секреты при обычном обновлении не перевыпускаются без причины.

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
- [`../../infrastructure/guests/910-infra-manager/README.md`](../../infrastructure/guests/910-infra-manager/README.md) — паспорт 910.
- [`830-recovery.md`](830-recovery.md) — восстановление.
