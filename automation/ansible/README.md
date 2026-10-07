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
        ├── ai_control/
        │   ├── tasks/main.yml
        │   └── templates/docker-compose.yml.j2
        ├── portal/
        │   ├── tasks/main.yml
        │   └── templates/
        └── infra_manager/
            └── tasks/
                ├── main.yml
                ├── persistence.yml
                ├── pve_access.yml
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
→ infra_manager, если guest.yaml содержит role: infra-manager
→ ai_control, если guest.yaml содержит role: ai-control
→ network_gateway, если guest.yaml содержит role: network-gateway
→ portal, если provision.yaml включает portal.local
→ guest_layout
```

Назначение:

- `linux_base` — устанавливает системные пакеты и настраивает доверие SSH к центру доступа OpenBao;
- `docker` — устанавливает и запускает Docker;
- `infra_manager` — настраивает постоянное состояние, доступы, Semaphore, OpenBao и управляющую среду гостя с ролью `infra-manager`;
- `ai_control` — разворачивает Hermes и Open WebUI через Docker Compose, создаёт их постоянные каталоги и локальные секреты и проверяет доступность обоих сервисов;
- `network_gateway` — настраивает сетевые службы гостя с ролью `network-gateway`;
- `portal` — разворачивает локальный Homepage из `portal.local`, применяет единое оформление, локальный фон, иконки, формирует опубликованные службы из вложенных `portal.targets` и стандартные действия обслуживания;
- `guest_layout` — создаёт только общие каталоги из `provision.components`, если они определены.

Для 109, 410 и 910 после их специализированной настройки применяется общая роль `portal`, потому что их `provision.yaml` включает `portal.local`. Затем выполняется общий `guest_layout`.

Постоянный файл `inventory` сейчас не хранится. `deploy-guest` определяет адрес выбранного гостя и запускает Ansible с одноузловым inventory через `-i <адрес>,`. Поэтому отдельный каталог `inventory/` не создаётся.

Термины `inventory`, `playbook` и `role` оставлены без перевода там, где они обозначают конкретные сущности Ansible.

Пустые каталоги и README-заглушки только ради сохранения структуры в Git не нужны.

## Основные правила

- Открытый ключ центра доступа OpenBao хранится на управляющей системе по пути `/etc/infra-manager/ca/ssh-client-ca.pub`.
- Роль `linux_base` устанавливает его гостю как `/etc/ssh/trusted-user-ca-keys.pem` и задаёт `TrustedUserCAKeys` отдельным файлом `/etc/ssh/sshd_config.d/40-infra-manager-user-ca.conf`.
- Полная настройка гостя требует наличия опубликованного открытого ключа; базовая фаза первоначального создания 910 может выполняться до инициализации OpenBao.
- Для совершенно нового обычного гостя `deploy-guest` создаёт отдельную одноразовую bootstrap-пару Ed25519. Открытый ключ передаётся OpenTofu только для первоначального Cloud-Init, а закрытый хранится лишь до завершения первичной настройки этого гостя и затем удаляется.
- После установки доверия к client CA административный вход выполняется только по краткоживущему пользовательскому SSH-сертификату OpenBao с principal `root`. Пара ключей для такого сертификата создаётся на время конкретной операции и удаляется после её завершения.
- Первоначальное создание 910 использует отдельный временный bootstrap-контур 990; его ключ не является постоянной identity 910 и удаляется после передачи управления.
- Постоянные RoleID/SecretID служебного доступа к OpenBao остаются только на PVE и в `infra-runtime` не монтируются.
- Межмашинный SSH использует отдельный ограниченный AppRole источника и одноразовый SSH OTP только для целей из `infrastructure/security/access.yaml`; ключ Ansible для этого не используется.
- На целевых гостях OTP должен проверяться через OpenBao helper и PAM по защищённому TLS-соединению. Подлинность SSH-сервера отдельно проверяется через host CA.
- Роль устанавливает OTP-клиент на источнике и OTP helper/PAM на цели в ходе `Deploy Guest`. `Sync SSH Access` не изменяет файлы гостя.
- Если гость уже доверяет опубликованному SSH CA, полный Ansible обязан войти по временному сертификату. Неудача такого входа считается ошибкой и не скрывается переходом на постоянный ключ.
- Постоянного `guest_ed25519` в 910 нет: новый гость получает только одноразовый bootstrap-ключ, а штатные повторные операции используют краткоживущие сертификаты OpenBao.
- Одноразовый bootstrap-ключ не используется для межмашинного SSH и не входит в постоянное резервируемое состояние 910.
- На целевой гостевой системе не требуется устанавливать полный управляющий Ansible; достаточно SSH и системных предпосылок используемых модулей.
- Конфигурация приложений и сервисов должна быть идемпотентной и повторяемой.
- Секреты, пароли, токены и закрытые ключи не хранятся в Git.
- Рабочее состояние и постоянные данные приложений не следует превращать в содержимое каталога Ansible; для них действуют отдельные правила резервного копирования и восстановления.
- Структура файлов внутри Linux-гостей следует [`../../docs/300-guests/350-linux-filesystem.md`](../../docs/300-guests/350-linux-filesystem.md).

## Связанные документы

- [`../../docs/700-security/720-ssh-access.md`](../../docs/700-security/720-ssh-access.md) — административные SSH-сертификаты, межмашинный OTP и проверка серверов.
- [`../../docs/700-security/750-access-contract.md`](../../docs/700-security/750-access-contract.md) — машинный источник разрешённых связей.
- [`../../docs/300-guests/330-guest-lifecycle.md`](../../docs/300-guests/330-guest-lifecycle.md) — граница ответственности OpenTofu и Ansible.
- [`../../docs/300-guests/350-linux-filesystem.md`](../../docs/300-guests/350-linux-filesystem.md) — правила размещения файлов и данных внутри Linux-гостей.
- [`../../infrastructure/guests/README.md`](../../infrastructure/guests/README.md) — требуемое состояние и файлы конкретных гостевых систем.
