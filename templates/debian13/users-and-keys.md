# Пользователи, SSH-ключи и доступ через Proxmox console

## 1. `ops` и `root`

`ops` — основной административный пользователь Linux VM.

```text
groups: ops, sudo, adm
password: locked
sudo: NOPASSWD
```

Пароль `root` также явно заблокирован. Рабочего и пустого пароля ни у `ops`, ни у `root` нет.

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

Root SSH запрещён полностью. Builder проверяет как синтаксис `sshd -t`, так и эффективные значения через `sshd -T`.

## 3. Основная Proxmox console

Начиная с Template-Version 4 основной Web Console используется в обычном VGA/noVNC режиме:

```text
vga: std
Proxmox Web UI → VM → Console → noVNC/VGA → tty1 → autologin ops
```

Autologin задаётся через:

```text
/etc/systemd/system/getty@tty1.service.d/autologin.conf
```

Это не password authentication. `agetty` автоматически запускает локальную сессию `ops` на `tty1`, а пароль `ops` остаётся locked.

Для нормального размера текста template использует обычное Debian-ядро `linux-image-amd64`, framebuffer и `console-setup`:

```text
FONTFACE="Fixed"
FONTSIZE="8x16"
```

Builder требует наличие валидного `fb0`, но не фиксирует конкретное разрешение. На текущем PVE фактически получается `1280x800`, и это значение выводится в отчёте сборки.

## 4. Резервная serial-консоль

Параллельно сохраняется:

```text
serial0: socket
xterm.js / qm terminal → ttyS0 → autologin ops
```

Override:

```text
/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
```

Serial-консоль нужна как резервный административный канал. Она не является основной Web Console при `vga: std`.

Не следует одновременно держать несколько клиентов, подключённых к одному `serial0`: serial — поток, а не framebuffer, и несколько одновременных terminal clients могут мешать друг другу.

## 5. Что означает доступ к Console

Следствие для обоих console-каналов:

```text
право открыть VM Console в Proxmox
= административный shell внутри гостевой VM
```

Так как `ops` имеет `NOPASSWD: sudo`, console access практически эквивалентен root-доступу к гостевой ОС. Proxmox ACL на `VM.Console` нужно выдавать только доверенным пользователям/ролям.

## 6. Template не содержит персональных credentials

В `tpl-debian13`:

```text
ops password = locked
root password = locked
ops authorized_keys = empty
```

Base template не содержит личных public/private keys и паролей администратора.

## 7. Личные SSH keys

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

## 8. Технические keys

Технические ключи разделяются по ролям:

```text
deploy_ed25519
backup_ed25519
ci_ed25519
```

Private key хранится там, откуда инициируется действие. Public key передаётся только тем VM, где эта роль нужна.

## 9. Cloud-Init и агент

Для стандартного автоматического развёртывания агенту достаточно public key, hostname/network параметров и права клонировать template.

Порядок:

```text
Full Clone
→ protection=0, если паспорт VM явно не требует защиты
→ hostname/name
→ ciuser=ops
→ sshkeys
→ network/DNS
→ qm cloudinit update
→ first start
```

Base template `9000` остаётся защищённым (`protection=1`), но обычные рабочие Full Clone по умолчанию должны иметь `protection=0`.

Если public key хранится в Git, агент может получить его из репозитория; private key при этом остаётся на административном устройстве.

## 10. SSH host keys VM

`/etc/ssh/ssh_host_*` идентифицируют сервер, а не пользователя. Перед превращением builder в template они удаляются. Каждый клон создаёт собственные host keys при первом запуске.

## 11. Backup technical private keys

Отдельную secret-backup инфраструктуру пока не создаём. Если technical private key находится внутри рабочей VM, полный Proxmox backup этой VM сохраняет его вместе с остальными данными. Такой backup считается чувствительным объектом.

## 12. Итоговая модель

```text
Base template
├── ops account
├── ops password locked
├── root password locked
├── authorized_keys empty
├── vga: std
├── tty1 autologin ops
├── regular Debian amd64 kernel + framebuffer
├── console font Fixed 8x16
├── serial0: socket
└── ttyS0 autologin ops (fallback)

        ↓ Full Clone + Cloud-Init before first boot

Concrete VM
├── one or more SSH public keys
├── hostname
├── network settings
└── protection=0 by default unless explicitly required

Administrative device
└── corresponding private SSH key
```

Три административных канала:

```text
1. SSH → ops + key
2. Proxmox Console/noVNC → VGA/tty1 → autologin ops
3. xterm.js или qm terminal → serial0/ttyS0 → autologin ops (fallback)
```
