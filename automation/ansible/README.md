# Ansible

`ansible/` предназначен для повторяемой настройки Linux VM/LXC после того, как гостевая система уже создана и доступна по административному SSH.

Граница ответственности описана в [`../../docs/300-guests/330-guest-lifecycle.md`](../../docs/300-guests/330-guest-lifecycle.md): OpenTofu управляет объектом VM/LXC в Proxmox, а Ansible отвечает за повторяемую настройку ОС и приложений после появления административного доступа.

## Источники данных

Для обычного Linux-гостя Ansible получает индивидуальные требования из:

~~~text
infrastructure/guests/<VMID>-<name>/provision.yaml
~~~

`provision.yaml` описывает, что должно быть установлено и настроено внутри конкретного гостя.

Playbook, roles и другая логика Ansible рядом с гостем не хранятся. Каталог вида:

~~~text
infrastructure/guests/<guest>/ansible/
~~~

в проекте не используется.

Общая логика Ansible находится только в `automation/ansible/`.

## Текущая структура

Общая логика Ansible организована через один playbook и набор roles:

```text
ansible.cfg
automation/
└── ansible/
    ├── playbooks/
    │   └── configure-guest.yml
    └── roles/
        ├── linux_base/
        │   └── tasks/
        │       ├── main.yml
        │       └── ssh_trust.yml
        ├── guest_layout/
        │   └── tasks/main.yml
        ├── docker/
        │   └── tasks/main.yml
        └── infra_manager/
            └── tasks/
                ├── main.yml
                ├── persistence.yml
                ├── pve_access.yml
                ├── ansible_access.yml
                ├── repository.yml
                ├── semaphore.yml
                ├── runtime.yml
                ├── openbao.yml
                └── verify.yml
```

Корневой `ansible.cfg` задаёт путь к общим roles:

```ini
[defaults]
roles_path = automation/ansible/roles
```

`configure-guest.yml` отвечает только за общий порядок: проверяет выбранного гостя и `provision.yaml`, проверяет Debian 13 и подключает нужные roles.

Порядок roles:

```text
linux_base
→ docker, если Docker требуется гостю
→ infra_manager, только для 910
→ guest_layout
```

Назначение:

- `linux_base` — устанавливает системные пакеты и настраивает доверие SSH к центру доступа OpenBao;
- `docker` — устанавливает и запускает Docker;
- `infra_manager` — настраивает постоянное состояние, доступы, Semaphore, OpenBao и управляющую среду 910;
- `guest_layout` — создаёт каталоги компонентов конкретного гостя.

Для 410 фактически применяются `linux_base` и `guest_layout`. Для 910 применяются все четыре roles.

Постоянный файл `inventory` сейчас не хранится. `deploy-guest` определяет адрес выбранного гостя и запускает Ansible с одноузловым inventory через `-i <адрес>,`. Поэтому отдельный каталог `inventory/` не создаётся.

Термины `inventory`, `playbook` и `role` оставлены без перевода там, где они обозначают конкретные сущности Ansible.

Пустые каталоги и README-заглушки только ради сохранения структуры в Git не нужны.

## Основные правила

- Открытый ключ центра доступа OpenBao хранится на управляющей системе по пути `/etc/infra-manager/ca/ssh-client-ca.pub`.
- Роль `linux_base` устанавливает его гостю как `/etc/ssh/trusted-user-ca-keys.pem` и задаёт `TrustedUserCAKeys` отдельным файлом `/etc/ssh/sshd_config.d/40-infra-manager-user-ca.conf`.
- Полная настройка гостя требует наличия опубликованного открытого ключа; базовая фаза первоначального создания 910 может выполняться до инициализации OpenBao.
- Для полной настройки `deploy-guest` создаёт одноразовую пару Ed25519, передаёт на PVE только открытый ключ и получает от OpenBao пользовательский SSH-сертификат с principal `root` и сроком 15 минут.
- Постоянные RoleID/SecretID служебного доступа к OpenBao остаются только на PVE и в `infra-runtime` не монтируются. Временный закрытый ключ существует только в служебном временном каталоге на время задания и удаляется после завершения команды.
- Если гость уже доверяет опубликованному SSH CA, полный Ansible обязан войти по временному сертификату. Неудача такого входа считается ошибкой и не скрывается переходом на постоянный ключ.
- Постоянный Ansible-ключ временно сохраняется только для первоначального входа в новый гость, где доверие к SSH CA ещё не установлено, а также для базовой стадии первоначального создания 910. Его удаление относится к заключительному этапу перехода.
- Ключ Ansible не совпадает с `pve_guest_ed25519` на стороне PVE и ключом AI Control.
- На целевой гостевой системе не требуется устанавливать полный управляющий Ansible; достаточно SSH и системных предпосылок используемых модулей.
- Конфигурация приложений и сервисов должна быть идемпотентной и повторяемой.
- Секреты, пароли, токены и закрытые ключи не хранятся в Git.
- Рабочее состояние и постоянные данные приложений не следует превращать в содержимое каталога Ansible; для них действуют отдельные правила резервного копирования и восстановления.
- Структура файлов внутри Linux-гостей следует [`../../docs/300-guests/350-linux-filesystem.md`](../../docs/300-guests/350-linux-filesystem.md).

## Связанные документы

- [`../../docs/700-security/720-ssh-access.md`](../../docs/700-security/720-ssh-access.md) — SSH-ключи и жизненный цикл секретов.
- [`../../docs/300-guests/330-guest-lifecycle.md`](../../docs/300-guests/330-guest-lifecycle.md) — граница ответственности OpenTofu и Ansible.
- [`../../docs/300-guests/350-linux-filesystem.md`](../../docs/300-guests/350-linux-filesystem.md) — правила `rootfs/` и размещения файлов и данных внутри Linux-гостей.
- [`../../infrastructure/guests/README.md`](../../infrastructure/guests/README.md) — требуемое состояние и файлы конкретных гостевых систем.
