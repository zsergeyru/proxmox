# Пользователи, SSH-ключи и доступ к Debian VM

## `ops` и `root`

`ops` — основной административный пользователь Linux VM:

```text
groups: ops, sudo, adm
password: locked
sudo: NOPASSWD
```

Пароль `root` также locked.

Штатная SSH policy:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

То есть сетевой административный доступ:

```text
SSH → ops + private key
```

## Template не содержит credentials

В `tpl-debian13`:

```text
ops password = locked
root password = locked
ops authorized_keys = empty
```

Ни personal keys, ни AI keys в template не зашиваются. Каждый конкретный clone получает public keys отдельно через Cloud-Init до первого запуска.

## Cloud-Init порядок

Для обычной managed Debian VM:

```text
Full Clone from 9000
→ protection=0, если guest.yaml не требует обратного
→ hostname/name
→ CPU/RAM/disk/network
→ ciuser=ops
→ sshkeys=<нужные public keys>
→ cloud-init update
→ first start
→ QEMU Agent/SSH/health check
```

Private key через Cloud-Init никогда не передаётся.

## Personal SSH keys

Personal key pair принадлежит устройству человека, например:

```text
proxmox_ops_laptop
proxmox_ops_desktop
proxmox_ops_phone
```

Private key хранится только на соответствующем устройстве. Public key может храниться в Git, если это удобно.

Private keys:

- не хранить в Git;
- не хранить в base template;
- не размещать на PVE как постоянное хранилище без необходимости.

## AI Control infrastructure identity

У `301-ai-control` есть собственная technical identity для прямого SSH в managed guests:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Пара создаётся **внутри `301`** общим platform-скриптом `prepare-ai-control.sh`, а не installer конкретного AI-агента.

Правила:

```text
private key
→ остаётся только внутри 301
→ не попадает в Git
→ не попадает в tpl-debian13

public key
→ используется при создании managed VM
→ передаётся пользователю ops через Cloud-Init до first boot
```

После установки конкретного агента он может использовать:

```text
301-ai-control
→ /opt/ai-control/ssh/ai_control_ed25519
→ SSH ops@managed-guest
```

Этот direct SSH предназначен для bootstrap, diagnostics, one-off и emergency действий. Повторяемая конфигурация после появления `311-dev-services` выполняется Ansible.

## GitHub identity AI Control

Для доступа к приватному инфраструктурному Git используется **другая** пара:

```text
/opt/ai-control/ssh/github_proxmox_repo_ed25519
/opt/ai-control/ssh/github_proxmox_repo_ed25519.pub
```

Она также создаётся общим `prepare-ai-control.sh` внутри `301`, но используется только для:

```text
git@github.com:zsergeyru/proxmox.git
```

Public key вручную регистрируется как GitHub Deploy Key. Если AI-агент должен выполнять push, Deploy Key получает `Allow write access`.

Не использовать GitHub key для SSH в инфраструктурные VM и не использовать infrastructure key как GitHub Deploy Key. Разделение identities уменьшает blast radius компрометации одного credential.

## Другие technical keys

Для независимых ролей допускаются отдельные пары, например:

```text
deploy_ed25519
backup_ed25519
ci_ed25519
```

Общий принцип тот же: private key находится там, откуда инициируется действие; public key получают только необходимые targets.

## SSH host keys VM

`/etc/ssh/ssh_host_*` идентифицируют сервер, а не клиента. Перед превращением builder в template они удаляются, поэтому каждый clone создаёт уникальные host keys при первом boot.

## Proxmox Console

Template-Version 4 использует основной Web Console:

```text
vga: std
Proxmox Web UI
→ noVNC/VGA
→ tty1
→ autologin ops
```

Fallback:

```text
serial0: socket
→ ttyS0
→ autologin ops
```

Autologin на локальной console не является password authentication; passwords `ops` и `root` остаются locked.

Поскольку `ops` имеет `NOPASSWD sudo`, право открыть VM Console фактически является административным доступом к гостю. Proxmox ACL на `VM.Console` выдаётся только доверенным identities.

## Backup и private keys

Если technical private key находится внутри рабочей VM, полный Proxmox backup этой VM содержит этот key. Поэтому backup `301-ai-control` считается чувствительным объектом: кроме SSH identities в нём находятся и другие control-plane secrets.

## Итоговая модель

```text
9000 tpl-debian13
├── ops account
├── passwords locked
└── authorized_keys empty

          ↓ Full Clone + Cloud-Init

managed VM
└── /home/ops/.ssh/authorized_keys
    ├── AI Control public key
    └── другие разрешённые public keys при необходимости

301-ai-control
└── /opt/ai-control/ssh/ai_control_ed25519
    └── private infrastructure identity

301-ai-control
└── /opt/ai-control/ssh/github_proxmox_repo_ed25519
    └── private GitHub-only identity
```

Таким образом private keys никогда не требуется копировать в создаваемую VM: clone получает только public half.
