# Пользователи, SSH-ключи и простой backup

Этот документ фиксирует модель пользователей и SSH-ключей для VM, создаваемых из `tpl-debian13`.

## 1. `root`

Учётная запись `root` как системная часть Debian **не удаляется**.

Она нужна самой ОС и для операций, выполняемых через `sudo`.

Удалённый SSH-login для `root` полностью запрещается:

```text
PermitRootLogin no
```

Прямой пользовательский вход под `root` не является штатным способом администрирования. Основной пользователь — `ops`.

## 2. `ops`

Основной административный пользователь всех обычных Linux VM:

```text
ops
```

Назначение:

- вход по SSH с public key;
- вход через Proxmox serial console по паролю;
- ручное администрирование;
- запуск административных команд через `sudo`;
- работа с Git, конфигами, логами и сервисами.

Базовые группы:

```text
ops
sudo
adm
```

Не добавлять `ops` заранее в специальные группы (`docker`, `www-data`, `backup`, `lxd` и т. п.). Такие права выдаются только конкретной VM по необходимости.

## 3. Два способа административного входа

Для простоты и аварийного доступа используются два независимых механизма:

```text
Proxmox serial console
→ ops + password

SSH
→ ops + private key на административном компьютере
```

Парольный SSH запрещён:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Таким образом наличие console password не открывает вход по SSH с этим паролем.

## 4. Console password

`ops` получает пароль через стандартный Proxmox Cloud-Init template configuration.

Пароль задаётся интерактивно во время сборки `tpl-debian13` и **не записывается в Git**.

Назначение пароля:

- вход через serial console Proxmox при проблемах с сетью;
- локальная диагностика VM;
- аварийный доступ к гостевой ОС.

Клоны по умолчанию наследуют один и тот же console password от template. Это осознанное упрощение домашней инфраструктуры. При необходимости для конкретной VM пароль можно заменить через её Cloud-Init параметры.

Пароль не должен передаваться в чат, документацию или Git.

## 5. Cloud-Init и `ops`

После сборки template содержит стандартные Cloud-Init defaults:

```text
ciuser = ops
network = DHCP
SSH public key = административный public key
console password = заданный при сборке пароль
```

При обычном клонировании эти параметры наследуются новой VM.

Cloud-Init также может использоваться для изменения hostname, IP/network configuration, DNS, SSH public keys и console password конкретного клона.

## 6. Личный SSH-ключ администратора

Личный SSH-ключ принадлежит **устройству**, а не конкретной VM.

Примеры:

```text
proxmox_ops_laptop
proxmox_ops_desktop
proxmox_ops_phone
```

Приватная часть хранится только на соответствующем устройстве:

```text
~/.ssh/proxmox_ops_laptop
```

Публичная часть:

```text
~/.ssh/proxmox_ops_laptop.pub
```

не является секретом.

При сборке template public key передаётся `create-template.sh`. Скрипт записывает его в стандартные Proxmox Cloud-Init параметры template. Поэтому новые клоны автоматически получают этот ключ для пользователя `ops`.

В гостевой VM Cloud-Init размещает public key в:

```text
/home/ops/.ssh/authorized_keys
```

### Правило

Личные private keys:

- не хранить в Git;
- не хранить в template;
- не хранить на Proxmox-хосте;
- резервировать вместе с данными соответствующего пользовательского устройства.

Public keys можно хранить в Git и использовать как воспроизводимую конфигурацию.

## 7. Дополнительные устройства

Если позже появится второй административный компьютер или телефон, для него создаётся отдельный key pair.

Public keys нескольких устройств можно добавить пользователю `ops` на конкретных VM или включить в следующую версию template.

Компрометация одного устройства тогда требует отзыва только его public key, а не замены одного общего private key на всех устройствах.

## 8. Пользователи сервисов

Пользователи конкретных сервисов не создаются в base template.

Примеры:

```text
gitea
jenkins
prometheus
postgres
backup
deploy
```

Они создаются только там, где реально нужны.

## 9. Технические SSH-ключи

Технический ключ принадлежит роли или сервису, а не человеку.

Примеры:

```text
deploy_ed25519
backup_ed25519
ci_ed25519
```

Не использовать один общий технический ключ для всех задач.

Private key хранится там, откуда инициируется действие.

Например:

```text
/home/deploy/.ssh/id_ed25519
/home/backup/.ssh/id_ed25519
```

Для CI/CD по возможности использовать встроенное secret/credentials storage соответствующей системы.

Права для файловых ключей:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519
chmod 644 ~/.ssh/id_ed25519.pub
```

## 10. Публичные технические ключи

Public keys можно хранить в Git и раскладывать на нужные VM.

Пример структуры проекта:

```text
ssh/
└── authorized_keys/
    ├── sergey-laptop.pub
    ├── deploy.pub
    └── backup.pub
```

На целевой VM public key обычно находится в:

```text
/home/<user>/.ssh/authorized_keys
```

## 11. SSH host keys самой VM

Host keys идентифицируют сам SSH-сервер, а не пользователя.

Они находятся примерно здесь:

```text
/etc/ssh/ssh_host_ed25519_key
/etc/ssh/ssh_host_rsa_key
...
```

Одинаковые host keys нельзя клонировать во все VM.

Перед превращением builder-VM в template host keys очищаются. Каждый клон при первом запуске должен создать собственные уникальные host keys.

## 12. Простой backup технических ключей

На первом этапе **не создаём отдельную secret-backup инфраструктуру**.

Если технический private key находится внутри VM, полный Proxmox backup этой VM сохраняет его вместе с остальными данными.

```text
VM
└── /home/deploy/.ssh/id_ed25519
        ↓
Proxmox backup
        ↓
восстановление VM вместе с ключом
```

Этого достаточно для первого этапа проекта.

Backup VM, содержащей private keys и другие секреты, сам является чувствительным объектом.

Поэтому:

- доступ к backup storage ограничивается;
- backup не хранится в Git или публичном доступе;
- используется доверенное внешнее storage;
- при компрометации backup находившиеся в нём private keys считаются скомпрометированными.

## 13. Что пока не вводим

Пока не создаём:

- отдельный central secret-backup server;
- автоматический сбор private keys со всех VM;
- Vault-подобную систему только ради SSH-ключей;
- общий private key для всей инфраструктуры.

К централизованному secret management можно вернуться позже, если инфраструктура существенно вырастет.

## 14. Итоговая модель

```text
Административный компьютер
├── private SSH key
└── console password знает пользователь
        │
        ├── SSH public key → Cloud-Init template → все обычные клоны
        └── console password → Cloud-Init template → все обычные клоны

VM
├── SSH: ops + key
└── Proxmox console: ops + password

Deploy/automation VM
└── private technical key
        ↓ public key

Целевые VM
└── authorized_keys технической роли
```

Основные правила:

1. `root` не удаляется, но SSH-login для него полностью запрещён.
2. `ops` — основной административный пользователь.
3. Proxmox console использует `ops` + password.
4. SSH использует `ops` + public key; password SSH запрещён.
5. Private SSH keys никогда не хранятся в Git или template.
6. Console password никогда не хранится в Git.
7. Public keys можно хранить в Git.
8. Личные ключи создаются отдельно для устройств.
9. Технические ключи создаются отдельно по ролям.
10. Технические private keys на первом этапе резервируются вместе с полной VM через Proxmox backup.
