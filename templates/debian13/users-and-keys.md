# Пользователи, SSH-ключи и доступ через Proxmox console

Этот документ фиксирует модель пользователей и административного доступа для VM из `tpl-debian13`.

## 1. `root`

`root` как системная учётная запись Debian не удаляется, но прямой удалённый вход запрещён:

```text
PermitRootLogin no
```

Основной административный пользователь — `ops`.

## 2. `ops`

`ops` создаётся в base template и используется для:

- SSH-входа по ключу;
- локального shell через Proxmox noVNC console;
- ручного администрирования;
- `sudo`;
- работы с Git, конфигами, логами и сервисами.

Базовые группы:

```text
ops
sudo
adm
```

`ops` имеет:

```text
ALL=(ALL) NOPASSWD:ALL
```

Специальные группы (`docker`, `www-data`, `backup` и т. п.) добавляются только там, где нужны.

## 3. Пароль `ops`

В самом `tpl-debian13` и в обычных клонах:

```text
ops password = locked
```

Пустого пароля нет. Рабочего пароля тоже нет.

Это означает:

- SSH по паролю невозможен;
- обычная password authentication для `ops` не используется;
- агенту не нужно получать, хранить или передавать пароль;
- `cipassword` в штатном deploy-сценарии не используется.

## 4. SSH-доступ

После клонирования через Proxmox Cloud-Init задаются:

```text
User: ops
SSH public key(s): ключи нужных устройств/ролей
Network: DHCP или статический IP
```

SSH-политика:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Штатный сетевой доступ:

```text
SSH
→ ops + private key на устройстве
→ соответствующий public key передан VM через Cloud-Init
```

## 5. Доступ через Proxmox console

Основная console VM в Proxmox использует:

```text
vga: std
```

и открывается через noVNC.

В Debian на `tty1` настроен autologin:

```text
Proxmox Web UI
→ VM → Console
→ noVNC
→ tty1
→ autologin ops
→ shell
```

Это **не вход с пустым паролем**. `agetty` запускает заранее доверенный локальный login для пользователя `ops`; password остаётся locked.

Таким образом право открыть console этой VM в Proxmox считается правом получить shell внутри гостевой ОС.

Поскольку `ops` имеет `NOPASSWD: sudo`, пользователь с Proxmox console access фактически получает административный доступ к этой VM. Поэтому права на console в Proxmox должны выдаваться только доверенным пользователям/ролям.

## 6. Serial console

Дополнительно сохраняется:

```text
serial0: socket
```

с обычным `serial-getty@ttyS0.service`.

На `serial0` autologin **не включается**. Он нужен как резервный диагностический канал для загрузки и troubleshooting.

## 7. Template не содержит персональных credentials

В самом `tpl-debian13`:

```text
ops password = locked
ops authorized_keys = empty
```

Template не содержит личных public keys, private keys или паролей администратора.

Public keys добавляются конкретным клонам через Cloud-Init.

## 8. Личные SSH-ключи

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

## 9. Пользователи сервисов

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

## 10. Технические SSH-ключи

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

## 11. Public keys в Git

Public keys можно хранить в Git и передавать нужным VM через Cloud-Init/Ansible.

Пример:

```text
ssh/
└── authorized_keys/
    ├── sergey-laptop.pub
    ├── deploy.pub
    └── backup.pub
```

## 12. SSH host keys самой VM

Host keys находятся в `/etc/ssh/ssh_host_*` и идентифицируют сам сервер.

Перед превращением builder-VM в template они удаляются. Каждый клон создаёт собственные уникальные host keys.

## 13. Простой backup технических ключей

Отдельную secret-backup инфраструктуру пока не создаём.

Если technical private key находится внутри VM, полный Proxmox backup этой VM сохраняет его вместе с остальными данными.

Backup VM с private keys считается чувствительным объектом и хранится только на доверенном storage.

## 14. Итоговая модель

```text
Base template
├── ops account
├── password locked
├── authorized_keys empty
├── tty1 autologin ops
└── serial0 without autologin

        ↓ Full Clone + Cloud-Init

Concrete VM
├── ops password still locked
├── one or more SSH public keys
├── hostname
└── IP/network settings

Administrative device
└── corresponding private SSH key
```

Два штатных канала административного доступа:

```text
1. SSH → ops + key
2. Proxmox noVNC → tty1 autologin ops
```

Основные правила:

1. `root` по SSH запрещён.
2. `ops` — основной администратор.
3. Пароль `ops` штатно не создаётся и остаётся locked.
4. SSH public keys задаются конкретной VM через Cloud-Init.
5. SSH по паролю запрещён.
6. `tty1` autologin доступен только через основную Proxmox console VM.
7. `serial0` сохраняется без autologin.
8. Proxmox console access считается административным доступом к гостевой VM.
9. Private SSH keys никогда не хранятся в Git/template.
10. Public keys можно хранить в Git.
11. Technical keys разделяются по ролям.
12. Technical private keys на первом этапе резервируются вместе с полной VM.
