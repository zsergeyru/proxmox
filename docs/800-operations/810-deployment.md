# Развёртывание инфраструктуры

**Тип:** эксплуатация  
**Статус:** проектируется  
**Назначение:** описать штатный путь от чистого PVE до работающего `910 infra-manager` и дальнейшего развёртывания обычных гостей.

## 1. Общая схема

```text
PVE
→ временный LXC 990 bootstrap-runner
→ отдельное состояние OpenTofu только для 910
→ LXC 910 infra-manager
→ общий provision.yaml + Ansible
→ Semaphore
→ OpenTofu / Ansible / Packer
→ обычные VM/LXC
```

PVE не становится постоянным узлом автоматизации.

## 2. Первоначальный контур 990

990 существует только во время первоначального создания или полного восстановления 910.

Он:

- получает временный PVE API-доступ;
- получает read-only доступ к проекту;
- запускает минимальный `bootstrap-runtime`;
- хранит отдельный OpenTofu state только для VMID 910;
- создаёт 910 через общий `deploy-guest`;
- запускает общий Ansible для настройки 910;
- после подтверждения готовности больше не нужен.

Постоянный OpenTofu state 910 никогда не содержит ресурс 910.

## 3. Единый путь настройки Linux-гостей

Для 910 и остальных Linux-гостей используется один путь:

```text
guest.yaml
→ OpenTofu
→ provision.yaml
→ automation/ansible/playbooks/configure-guest.yml
```

Отдельного `setup.sh` для 910 больше нет.

Если для 910 требуется новая возможность, она добавляется в общий Ansible-механизм или реализуется узкой служебной Python-командой, которую вызывает Ansible.

## 4. Фазы deploy-guest для 910

Обычный гость обычно проходит весь цикл одним вызовом.

Для 910 первоначальный контур использует три общие фазы:

```text
1. --infrastructure-only
   OpenTofu создаёт или сверяет LXC 910

2. --provision-base-only
   Ansible применяет базовую часть provision.yaml
   и устанавливает системные пакеты

3. --provision-only
   после передачи первоначальных доступов
   Ansible полностью применяет provision.yaml
```

Такое разделение нужно только потому, что PVE CA и постоянный PVE API credential можно передать лишь после появления самого LXC 910.

## 5. Передача первоначальных данных

После создания 910 PVE передаёт:

- PVE CA;
- постоянный PVE API credential 910;
- read-only GitHub Deploy Key;
- рабочую копию проекта.

Временный секрет PVE API сначала размещается по пути:

```text
/root/.infra-manager-bootstrap/pve-api.env
```

Общий Ansible сохраняет его в:

```text
/etc/infra-manager/secrets/pve-api.env
```

и удаляет временную копию.

## 6. Полная настройка 910 через provision.yaml

`infrastructure/guests/910-infra-manager/provision.yaml` определяет:

- системные пакеты;
- Docker;
- постоянные каталоги;
- Ansible SSH-идентичность;
- CA bundle;
- Compose;
- `infra-runtime`;
- Semaphore;
- OpenTofu input;
- служебные команды;
- проверки готовности.

Общий Ansible применяет эти требования.

## 7. Docker и infra-runtime

Docker устанавливается из официального репозитория Docker.

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

Отдельный remote Runner для одного домашнего 910 не используется.

## 8. Semaphore

Первая версия автоматически поддерживает:

- проект `Proxmox Infrastructure`;
- ключ `GitHub project read-only`;
- Git-репозиторий `proxmox`;
- набор переменных `OpenTofu PVE`;
- `OpenTofu Plan`;
- `Build Template 9000`;
- `Deploy Guest 410`.

Синхронизация этих объектов выполняется узкой Python-командой:

```text
python3 -m infra_manager semaphore-project
```

Она вызывается из общего Ansible и не является отдельным механизмом настройки 910.

## 9. OpenTofu

Постоянный state 910:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Он:

- не хранится в Git;
- входит в резервное копирование;
- не должен автоматически пересоздаваться пустым поверх существующей инфраструктуры;
- никогда не содержит ресурс VMID 910.

Для временного 990 используется отдельный state.

## 10. Проверка готовности

После полного Ansible выполняется:

```bash
infra-manager-status --full
```

Проверяются:

- Docker и Compose;
- постоянные файлы;
- `infra-runtime`;
- Semaphore;
- OpenTofu, Packer и Ansible;
- Git-доступ Semaphore;
- PVE API;
- полный контракт PVE-прав.

Только после этого первоначальный контур может считаться успешно завершённым.

## 11. Первичный пароль Semaphore

Пароль создаётся один раз и хранится:

```text
/etc/infra-manager/secrets/initial-admin-password
```

Права файла — `0600`.

Общий Ansible не печатает пароль и не записывает его в журнал.

Получить пароль от root внутри 910 можно явно:

```bash
cat /etc/infra-manager/secrets/initial-admin-password
```

## 12. Обновление 910

После завершения перехода на новый публичный bootstrap повторное применение должно использовать тот же общий путь:

```text
обновить проект
→ сверить инфраструктуру 910
→ повторно применить provision.yaml общим Ansible
→ синхронизировать Semaphore
→ infra-manager-status --full
```

Постоянные секреты при обычном обновлении не перевыпускаются.

## 13. Завершение временного контура

После успешного `infra-manager-status --full` bootstrap обязан:

```text
удалить /etc/bootstrap-runner/secrets
→ удалить /var/lib/bootstrap-runner/opentofu/state
→ удалить ACL временного token root@pam!bootstrap-runner
→ удалить сам token
→ остановить и удалить LXC 990
→ удалить скачанный bootstrap LXC-template, если он был создан именно bootstrap
```

Если любой этап до итоговой проверки 910 завершается ошибкой, 990 не удаляется автоматически: он остаётся для диагностики и безопасного повторного запуска.

Повторный обычный запуск при уже существующем 910 не создаёт новый OpenTofu state для 910. Он проверяет существующий объект и повторно применяет `provision.yaml` через общий Ansible.

## 14. Связанные документы

- `../200-pve/210-host-bootstrap.md` — первоначальный контур;
- `../700-security/710-pve-access.md` — доступ 910 к PVE;
- `../../infrastructure/guests/910-infra-manager/README.md` — паспорт 910;
- `830-recovery.md` — восстановление.
