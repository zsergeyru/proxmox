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

## 6. Template не содержит credentials

В `tpl-debian13`:

```text
ops password = locked
root password = locked
ops authorized_keys = empty
```

Base template не содержит личных или машинных public/private keys и паролей администратора.

Это принципиально: SSH identity возникает у конкретного управляющего устройства/VM, а не внутри общего template.

## 7. Личные SSH keys

Key pair принадлежит устройству:

```text
proxmox_ops_laptop
proxmox_ops_desktop
proxmox_ops_phone
```

Private key хранится только на соответствующем устройстве.

Public key не является секретом и при необходимости может храниться в Git.

Private keys:

- не хранить в Git;
- не хранить в base template;
- не размещать на PVE-хосте как постоянное хранилище.

## 8. AI Control infrastructure key

При zero-day bootstrap `301-ai-control` **внутри самой VM 301** создаёт собственную пару:

```text
/home/ops/.ssh/ai_control_ed25519
/home/ops/.ssh/ai_control_ed25519.pub
```

Назначение:

```text
private key
→ остаётся только в 301
→ используется Hermes для прямого SSH в managed guests

public key
→ передаётся новым Debian VM через Cloud-Init
→ попадает в /home/ops/.ssh/authorized_keys конкретного гостя
```

Таким образом, агент не должен «откуда-то получить» заранее созданный private key: ключ рождается внутри control plane при его bootstrap и с этого момента становится его машинной identity.

Private key не копируется обратно на PVE и не выводится bootstrap-скриптом.

## 9. Отдельный GitHub Deploy Key

Доступ к приватному `zsergeyru/proxmox` не смешивается с SSH-доступом к VM.

`301` создаёт вторую пару:

```text
/home/ops/.ssh/github_proxmox_ed25519
/home/ops/.ssh/github_proxmox_ed25519.pub
```

Она используется **только** для:

```text
git@github.com:zsergeyru/proxmox.git
```

Zero-day bootstrap выводит только `github_proxmox_ed25519.pub`. Оператор вручную регистрирует его как Deploy Key репозитория. Для автономного `commit/push` включается `Allow write access`.

GitHub PAT для базового bootstrap не требуется.

Разделение ключей обязательно:

```text
ai_control_ed25519
→ SSH в домашние VM

github_proxmox_ed25519
→ SSH transport GitHub
```

Компрометация GitHub Deploy Key не должна автоматически давать SSH-доступ к нашим гостевым ОС.

## 10. Другие технические keys

Другие машинные роли при необходимости получают отдельные ключи, например:

```text
deploy_ed25519
backup_ed25519
ci_ed25519
```

Private key хранится там, откуда инициируется действие. Public key передаётся только тем VM, где эта роль нужна.

После появления `311-dev-services` Ansible может получить отдельный `deploy_ed25519`; это не отменяет прямой `ai_control_ed25519` у `301` для bootstrap/diagnostic/emergency задач.

## 11. Cloud-Init и AI-агент

Для стандартного автоматического развёртывания Hermes берёт public infrastructure key локально из:

```text
/home/ops/.ssh/ai_control_ed25519.pub
```

Порядок:

```text
Full Clone 9000
→ поместить guest в managed pool
→ protection=0, если паспорт VM явно не требует защиты
→ hostname/name
→ ciuser=ops
→ sshkeys=<содержимое ai_control_ed25519.pub>
→ network/DNS
→ Cloud-Init update
→ first start
→ QEMU Agent/SSH/health check
```

Base template `9000` остаётся защищённым (`protection=1`), но обычные рабочие Full Clone по умолчанию должны иметь `protection=0`.

Public key для этого сценария не обязан храниться в Git: он доступен непосредственно внутри `301` рядом с соответствующим private key.

## 12. SSH host keys VM

`/etc/ssh/ssh_host_*` идентифицируют сервер, а не пользователя. Перед превращением builder в template они удаляются. Каждый клон создаёт собственные host keys при первом запуске.

## 13. Backup private keys

Если technical private key находится внутри рабочей VM, полный Proxmox backup этой VM сохраняет его вместе с остальными данными. Такой backup считается чувствительным объектом.

Для `301-ai-control` это особенно важно: его backup содержит как минимум infrastructure private key, GitHub Deploy Key и credentials AI/MCP, появившиеся после bootstrap.

## 14. Итоговая модель

```text
Base template 9000
├── ops account
├── ops password locked
├── root password locked
├── authorized_keys empty
├── никаких персональных/машинных SSH keys
├── vga: std
├── tty1 autologin ops
├── regular Debian amd64 kernel + framebuffer
├── serial0: socket
└── ttyS0 autologin ops (fallback)

        ↓ Full Clone 9000 → 301

301-ai-control
├── ai_control_ed25519
│   └── identity для SSH в managed guests
├── github_proxmox_ed25519
│   └── Deploy Key только для private Git
└── Hermes + Proximo

        ↓ Full Clone + Cloud-Init before first boot

Managed Debian VM
├── ops authorized_keys содержит ai_control_ed25519.pub
├── hostname/network
└── protection=0 по умолчанию
```

Административные каналы обычной VM:

```text
1. SSH → ops + соответствующий key
2. Proxmox Console/noVNC → VGA/tty1 → autologin ops
3. xterm.js или qm terminal → serial0/ttyS0 → autologin ops (fallback)
```
