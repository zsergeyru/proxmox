# Пользователи, SSH-ключи и простой backup

Этот документ фиксирует модель пользователей и SSH-доступа для VM из `tpl-debian13`.

## 1. `root`

`root` как системная учётная запись Debian не удаляется, но прямой удалённый вход запрещён:

```text
PermitRootLogin no
```

Основной административный пользователь — `ops`.

## 2. `ops`

`ops` создаётся в base template и используется для:

- SSH-входа по ключу;
- ручного администрирования;
- `sudo`;
- работы с Git, конфигами, логами и сервисами.

Базовые группы:

```text
ops
sudo
adm
```

Специальные группы (`docker`, `www-data`, `backup` и т. п.) добавляются только там, где нужны.

## 3. Template не содержит credentials

В самом `tpl-debian13`:

```text
ops password = locked
ops authorized_keys = empty
```

Template не содержит личных public keys, private keys или рабочего пароля администратора.

Это намеренно: credentials относятся к конкретной VM/устройству, а не к универсальному template.

## 4. Cloud-Init конкретной VM

После клонирования через Proxmox Cloud-Init задаются:

```text
User: ops
SSH public key(s): ключи нужных устройств/ролей
Network: DHCP или статический IP
```

Пароль `ops` в штатном сценарии не задаётся.

SSH по паролю запрещён:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

## 5. Основной административный доступ

```text
SSH
→ ops + private key на устройстве
→ соответствующий public key передан VM через Cloud-Init
```

Serial console Proxmox остаётся диагностическим каналом: через неё можно наблюдать загрузку и сообщения системы, но штатного парольного login для `ops` нет.

Если SSH недоступен, но QEMU Guest Agent работает, для аварийной диагностики и команд можно использовать `qm guest exec`. Если одновременно недоступны сеть и Guest Agent, используется recovery/single-user сценарий через Proxmox.

## 6. Личные SSH-ключи

Личный SSH key pair принадлежит **устройству**, а не VM.

Примеры:

```text
proxmox_ops_laptop
proxmox_ops_desktop
proxmox_ops_phone
```

Private key хранится только на соответствующем устройстве:

```text
~/.ssh/proxmox_ops_laptop
```

Public key:

```text
~/.ssh/proxmox_ops_laptop.pub
```

передаётся нужным VM через Cloud-Init и попадает в:

```text
/home/ops/.ssh/authorized_keys
```

Правила:

- private keys не хранить в Git;
- private keys не хранить в template;
- private keys не хранить на Proxmox-хосте без отдельной необходимости;
- public keys можно хранить в Git;
- для нового устройства создавать отдельный key pair.

## 7. Пароли

Для обычных VM пароль `ops` **не используется** и остаётся заблокированным.

Это упрощает автоматическое развёртывание: агенту не нужно получать, хранить или передавать пароль. Доступ строится только на SSH public keys, передаваемых через Cloud-Init.

Если в будущем для отдельной VM действительно понадобится локальный пароль, это должно быть отдельным решением для этой VM, а не свойством базового template.

## 8. Пользователи сервисов

Service users не создаются в base template.

Примеры:

```text
gitea
jenkins
prometheus
postgres
backup
deploy
```

Они создаются только по необходимости.

## 9. Технические SSH-ключи

Технический ключ принадлежит роли или сервису:

```text
deploy_ed25519
backup_ed25519
ci_ed25519
```

Не использовать один общий technical key для всех задач.

Private key хранится там, откуда инициируется действие, например:

```text
/home/deploy/.ssh/id_ed25519
/home/backup/.ssh/id_ed25519
```

Для CI/CD предпочтительно использовать secret/credentials storage соответствующей системы.

Права файлов:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519
chmod 644 ~/.ssh/id_ed25519.pub
```

## 10. Публичные технические ключи

Public keys можно хранить в Git и передавать нужным VM через Cloud-Init/Ansible.

Пример:

```text
ssh/
└── authorized_keys/
    ├── sergey-laptop.pub
    ├── deploy.pub
    └── backup.pub
```

## 11. SSH host keys самой VM

Host keys находятся в `/etc/ssh/ssh_host_*` и идентифицируют сам сервер.

Перед превращением builder-VM в template они удаляются. Каждый клон создаёт собственные уникальные host keys.

## 12. Простой backup технических ключей

Отдельную secret-backup инфраструктуру пока не создаём.

Если technical private key находится внутри VM, полный Proxmox backup этой VM сохраняет его вместе с остальными данными.

Backup VM с private keys считается чувствительным объектом и хранится только на доверенном storage.

## 13. Итоговая модель

```text
Base template
├── ops account
├── password locked
└── authorized_keys empty

        ↓ Full Clone + Cloud-Init

Concrete VM
├── ops password still locked
├── one or more SSH public keys
├── hostname
└── IP/network settings

Administrative device
└── corresponding private SSH key
```

Основные правила:

1. `root` по SSH запрещён.
2. `ops` — основной администратор.
3. Template не содержит персональных credentials.
4. Пароль `ops` штатно не создаётся.
5. SSH public keys задаются конкретной VM через Cloud-Init.
6. SSH по паролю запрещён.
7. Private SSH keys никогда не хранятся в Git/template.
8. Public keys можно хранить в Git.
9. Technical keys разделяются по ролям.
10. Technical private keys на первом этапе резервируются вместе с полной VM.
