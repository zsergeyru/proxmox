# 910 infra-deployer

`910 infra-deployer` — специальный постоянный LXC, из которого выполняется штатное развёртывание и настройка инфраструктуры проекта.

Он создаётся и восстанавливается только публичным `bootstrap-pve.sh` и не входит в собственное OpenTofu state.

## Назначение

Внутри `910` работают:

- Semaphore Server;
- Semaphore Runner;
- OpenTofu;
- Ansible;
- Packer;
- Git и вспомогательные средства инфраструктурного контура.

Схема:

```text
PVE
└─ bootstrap-pve.sh
   └─ 910 infra-deployer
      ├─ Semaphore
      ├─ OpenTofu
      ├─ Ansible
      ├─ Packer
      └─ Git
```

## Особенность гостя

`910` является исключением из обычного гостевого контура:

- его виртуальным объектом владеет bootstrap;
- OpenTofu не создаёт и не изменяет `910`;
- `910` не входит в обычный пул `managed`;
- штатное управление PVE выполняется через HTTPS API;
- постоянный root SSH с `910` на PVE не используется.

Точные параметры создания виртуального объекта принадлежат публичному `zsergeyru/proxmox-bootstrap`, потому что именно он создаёт `910` до появления доступа к закрытому проекту.

## Состав системы

Базовая Debian-система содержит только необходимую основу: Docker, Docker Compose, Git, SSH, `curl`, CA-сертификаты и средства диагностики.

Для первой версии используются:

```text
Semaphore Server v2.18.30
SQLite
Semaphore Runner v2.18.30
OpenTofu 1.12.6
Packer 1.16.1
```

Runner собирается поверх официального образа Semaphore. В нём включена строгая проверка SSH host keys, добавлены OpenTofu и Packer, а Python-зависимости инфраструктуры устанавливаются через `requirements.txt`.

После первого запуска автоматически создаётся проект Semaphore `Proxmox Infrastructure`, его Key Store и запись закрытого репозитория `zsergeyru/proxmox`.

## Постоянные данные

Основные постоянные области:

```text
/etc/infra-deployer/
/var/lib/infra-deployer/
/opt/infra-deployer/
```

К критичным данным относятся:

- база Semaphore;
- ключ шифрования Semaphore;
- OpenTofu state;
- постоянные инфраструктурные секреты;
- локальная конфигурация, которую нельзя восстановить из Git.

Git-копии, кэш заданий, пакеты и образы контейнеров должны быть воспроизводимыми и не считаются критичными данными.

## Доступы

Для Proxmox используется отдельная идентичность:

```text
infra-deployer@pve!automation
```

Её права определяются общей моделью доступа Proxmox.

GitHub Deploy Key создаётся внутри `910` и используется только для чтения `zsergeyru/proxmox`.

Ansible использует отдельную техническую SSH-идентичность для управляемых Linux-гостей.

Постоянные секреты не хранятся в Git.

В `/etc/infra-deployer/secrets/` находятся защищённые восстановительные копии bootstrap credentials. Рабочие Git, PVE API и Ansible SSH credentials также создаются в зашифрованном Semaphore Key Store.

## OpenTofu state

Для первой версии состояние хранится локально:

```text
/var/lib/infra-deployer/opentofu/state/proxmox.tfstate
```

Оно считается чувствительным, резервируется и не должно одновременно изменяться несколькими заданиями.

Сам `910` в этом state отсутствует.

## Восстановление

Потерянный `910` восстанавливается через:

```text
bootstrap-pve.sh --recover
```

После создания контейнера восстанавливаются постоянные данные и проверяется OpenTofu state.

Если state потерян, нельзя начинать обычный `tofu apply` с пустым состоянием поверх существующей инфраструктуры. Сначала выполняется восстановление state или осознанный импорт объектов.

## Границы

`910` не используется для пользовательских приложений, Home Assistant, MQTT, Zigbee2MQTT, ESPHome, AI-сервисов, Frigate, обычной разработки или общего файлового хранилища.

## Связанные документы

- [`../../docs/200-pve/210-host-bootstrap.md`](../../docs/200-pve/210-host-bootstrap.md) — создание и восстановление `910`.
- [`../../docs/700-security/710-pve-access.md`](../../docs/700-security/710-pve-access.md) — права Proxmox.
- [`../../docs/700-security/720-ssh-access.md`](../../docs/700-security/720-ssh-access.md) — SSH-доступ.
- [`../../docs/800-operations/830-recovery.md`](../../docs/800-operations/830-recovery.md) — общие правила восстановления.
- [`decisions.md`](decisions.md) — принятые решения по этому гостю.
- `zsergeyru/proxmox-bootstrap/bootstrap-pve.sh` — машинный контракт создания `910`.