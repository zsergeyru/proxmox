# Пользователи, SSH-ключи и доступ через Proxmox console

## 1. `ops`

`ops` — основной административный пользователь Linux VM.

```text
groups: ops, sudo, adm
password: locked
sudo: NOPASSWD
```

Рабочего и пустого пароля нет.

## 2. SSH

Штатный сетевой доступ:

```text
SSH → ops + private key
```

На каждой VM public key передаётся через Cloud-Init **до первого запуска**.

SSH policy:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Root SSH запрещён полностью.

## 3. Основная Proxmox console

Начиная с Template-Version 3 основная VM console — настоящая текстовая serial-консоль:

```text
vga: serial0
serial0: socket
Proxmox Web UI → Console → xterm.js → ttyS0 → autologin ops
```

Autologin задаётся через:

```text
/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
```

Это не password authentication. `agetty` автоматически запускает локальную сессию `ops` на `ttyS0`, а пароль `ops` остаётся locked.

Следствие:

```text
право открыть VM Console в Proxmox
= административный shell внутри гостевой VM
```

Так как `ops` имеет `NOPASSWD: sudo`, console access практически эквивалентен root-доступу к гостевой ОС. Proxmox ACL на `VM.Console` нужно выдавать только доверенным пользователям/ролям.

## 4. tty1/noVNC

В v3 специальный autologin на `tty1` не настраивается. Framebuffer/noVNC больше не является основной административной консолью шаблона.

Основной интерфейс для работы через Proxmox Web UI:

```text
xterm.js / serial0
```

## 5. Template не содержит персональных credentials

В `tpl-debian13`:

```text
ops password = locked
ops authorized_keys = empty
```

Base template не содержит личных public/private keys и паролей администратора.

## 6. Личные SSH keys

Key pair принадлежит устройству:

```text
proxmox_ops_laptop
proxmox_ops_desktop
proxmox_ops_phone
```

Private key хранится только на соответствующем устройстве.

Public key не является секретом и может храниться в Git, например:

```text
ssh/
└── authorized_keys/
    ├── sergey-laptop.pub
    ├── sergey-desktop.pub
    └── deploy.pub
```

Private keys:

- не хранить в Git;
- не хранить в base template;
- не передавать AI-агенту без отдельной необходимости;
- не размещать на PVE-хосте как постоянное хранилище.

## 7. Технические keys

Технические ключи разделяются по ролям:

```text
deploy_ed25519
backup_ed25519
ci_ed25519
```

Private key хранится там, откуда инициируется действие. Public key передаётся только тем VM, где эта роль нужна.

Не использовать один общий private key для всех сервисов и пользователей.

## 8. Cloud-Init и агент

Для стандартного автоматического развёртывания агенту достаточно public key, hostname/network параметров и права клонировать template.

Секретный пароль не нужен.

Порядок:

```text
clone
→ sshkeys
→ network/DNS
→ qm cloudinit update
→ first start
```

Если public key хранится в Git, агент может получить его из репозитория; private key при этом остаётся на административном устройстве.

## 9. SSH host keys VM

`/etc/ssh/ssh_host_*` идентифицируют сервер, а не пользователя.

Перед превращением builder в template они удаляются. Каждый клон должен создать собственные host keys при первом запуске.

## 10. Backup technical private keys

Отдельную secret-backup инфраструктуру пока не создаём.

Если technical private key находится внутри рабочей VM, полный Proxmox backup этой VM сохраняет его вместе с остальными данными. Такой backup считается чувствительным объектом.

## 11. Итоговая модель

```text
Base template
├── ops account
├── password locked
├── authorized_keys empty
├── serial0: socket
├── vga: serial0
└── ttyS0 autologin ops

        ↓ Full Clone + Cloud-Init before first boot

Concrete VM
├── one or more SSH public keys
├── hostname
└── network settings

Administrative device
└── corresponding private SSH key
```

Два штатных административных канала:

```text
1. SSH → ops + key
2. Proxmox xterm.js → serial0/ttyS0 → autologin ops
```
