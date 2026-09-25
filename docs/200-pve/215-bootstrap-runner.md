# 990 bootstrap-runner

**Тип:** спецификация  
**Статус:** принято к реализации  
**Назначение:** определить временный LXC, который разворачивает и обслуживает виртуальный объект 910 стандартными средствами закрытого проекта.

## 1. Роль

990 не является постоянным сервисом.

~~~text
public bootstrap
↓
990 bootstrap-runner
↓
OpenTofu + Ansible
↓
910 infra-manager
↓
удаление 990
~~~

В каталоге infrastructure/guests/ отдельный 990-bootstrap-runner не создаётся: этот каталог предназначен для постоянных гостевых систем.

## 2. Параметры LXC

~~~text
VMID:         990
hostname:     bootstrap-runner
ОС:           Debian 13 amd64
тип:          unprivileged LXC
cores:        2
memory:       2048 MiB
swap:         512 MiB
rootfs:       local-lvm:16 GiB
bridge:       vmbr0
IPv4:         DHCP
onboot:       false
protection:   false
pool managed: нет
features:     container-host
tags:         bootstrap-runner;temporary
~~~

container-host означает необходимые низкоуровневые возможности LXC для запуска Docker; конкретные флаги не являются публичным контрактом 990.

## 3. Среда выполнения

После получения закрытого проекта 990 поднимает Docker и использует тот же образ infra-runtime, который используется инфраструктурным контуром 910.

Semaphore Server внутри 990 не запускается. Образ используется только как одноразовая среда выполнения OpenTofu, Ansible и Python-кода проекта.

Версии OpenTofu, Ansible и Python-зависимостей не задаются отдельно в public bootstrap.

## 4. Рабочие каталоги

~~~text
/var/lib/bootstrap-runner/
├── opentofu/
│   └── state/
│       └── proxmox.tfstate
├── ssh/
└── work/

/etc/bootstrap-runner/
├── ca/
└── secrets/

/opt/bootstrap-runner/repo/
~~~

После успешного завершения эти данные удаляются вместе с 990.

## 5. Отдельный state 910

990 управляет отдельным OpenTofu state, содержащим только виртуальный объект 910.

Он не читает и не изменяет основной OpenTofu state внутри 910.

Каноническая копия хранится на PVE:

~~~text
/var/lib/proxmox-bootstrap/state/910.tfstate
~~~

Рабочая копия внутри 990:

~~~text
/var/lib/bootstrap-runner/opentofu/state/proxmox.tfstate
~~~

Перед удалением 990 итоговый state обязан быть сохранён обратно на PVE.

Если операция завершилась ошибкой, 990 автоматически не удаляется: его рабочий state может быть новее сохранённой на PVE копии.

## 6. PVE-доступ

990 использует отдельную временную идентичность:

~~~text
root@pam!bootstrap-runner
~~~

Она существует только на время bootstrap.

Постоянная идентичность 910 root@pam!infra-manager остаётся отдельной и не используется 990 как собственный credential.

После подтверждённого успеха token 990 и его ACL удаляются до удаления контейнера.

## 7. Git-доступ

990 получает только read-only доступ к выбранной ревизии закрытого проекта.

Закрытая Git-копия внутри 990 временная и удаляется вместе с контейнером.

## 8. Первоначальный SSH к 910

Для Ansible требуется первоначальный доступ в вновь созданный 910.

Этот доступ является bootstrap-credential и не должен автоматически становиться постоянным универсальным ключом управления гостями.

Точный механизм установки и удаления bootstrap SSH key реализуется вместе с LXC deploy-guest. До этого private key не хранится на PVE как постоянный общий SSH-ключ гостей.

## 9. Последовательность закрытого runner

Целевая последовательность:

~~~text
проверить, что выполняемся внутри CTID 990
→ проверить закрытый Git checkout
→ установить/проверить Docker
→ собрать infra-runtime из проекта
→ проверить PVE CA и bootstrap credential
→ принять bootstrap-state 910
→ построить effective state только для VMID 910
→ OpenTofu plan
→ OpenTofu apply
→ подготовить первоначальный SSH-доступ
→ Ansible provision 910
→ проверить 910
→ подготовить state к возврату на PVE
~~~

Runner не создаёт и не удаляет сам 990.

## 10. Повторный запуск и ошибка

Если предыдущий 990 остался после ошибки, повторный запуск сначала использует его рабочее состояние.

Старая копия state с PVE не должна молча перезаписывать более новый state оставшегося 990.

Автоматическое разрушительное пересоздание 910 после ошибки запрещено.

## 11. Удаление

Успешный 990 не должен жить постоянно.

Удаление разрешено только после:

- успешной проверки 910;
- экспорта bootstrap-state на PVE;
- передачи всех обязательных данных 910;
- отзыва временного PVE token 990.

После этого:

~~~text
stop 990
→ destroy 990
~~~

При ошибке 990 сохраняется для диагностики и безопасного продолжения.

## 12. Реализация в закрытом репозитории

Закрытая логика размещается отдельно:

~~~text
scripts/bootstrap-runner/
├── README.md
├── run.sh
└── deploy-910.py
~~~

Основная логика создания гостя остаётся в infra_manager.guest_deploy. bootstrap-runner только подготавливает временное окружение и вызывает общий механизм в режиме отдельного состояния 910.

## 13. Связанные документы

- [210-host-bootstrap.md](210-host-bootstrap.md) — public bootstrap.
- [220-host-configuration.md](220-host-configuration.md) — постоянное состояние PVE.
- [../300-guests/330-guest-lifecycle.md](../300-guests/330-guest-lifecycle.md) — общий жизненный цикл гостей.
- [../700-security/730-openbao.md](../700-security/730-openbao.md) — OpenBao и автоматический unseal.
- [../../infrastructure/guests/910-infra-manager/README.md](../../infrastructure/guests/910-infra-manager/README.md) — паспорт 910.
