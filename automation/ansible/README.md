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

## Целевая структура

Текущая общая точка входа:

```text
automation/ansible/
├── playbooks/
│   └── configure-guest.yml
├── inventory/    # узлы, группы и переменные инвентаря
└── roles/        # переиспользуемые роли
```

`configure-guest.yml` читает `provision.yaml` выбранного гостя и применяет его требования к операционной системе.

Для 410 используется:

```text
guest=410-ai-control
→ infrastructure/guests/410-ai-control/provision.yaml
→ playbooks/configure-guest.yml
```

По мере появления повторяемой специализированной логики она должна выноситься из playbook в общие roles, а не копироваться в отдельные playbook каждого гостя.

Термины `inventory`, `playbook` и `role` оставлены без перевода там, где они обозначают конкретные сущности Ansible.

Пустые каталоги и README-заглушки только ради сохранения структуры в Git не нужны. Каталог создаётся тогда, когда в нём появляется реальный inventory, playbook или role.

## Основные правила

- Целевые Linux-гости управляются по SSH отдельным ключом Ansible.
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
