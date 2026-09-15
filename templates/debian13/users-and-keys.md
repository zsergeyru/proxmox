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

Ни personal keys, ни deployer/AI/Ansible keys в template не зашиваются. Каждый конкретный clone получает нужные public keys отдельно через Cloud-Init до первого запуска.

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

## Host-side guest-bootstrap identity

Private PVE Stage 1 создаёт отдельную technical identity для первичного SSH-доступа `deploy-guest` к создаваемым Linux-гостям:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Правила:

```text
private key
→ остаётся только на PVE
→ принадлежит host-side deployer/pvedeploy
→ не попадает в Git
→ не попадает в tpl-debian13
→ не передаётся гостю

public key
→ передаётся конкретному создаваемому гостю
→ для VM попадает в ops authorized_keys через Cloud-Init
→ для LXC передаётся штатным механизмом создания/bootstrap контейнера
```

Этот ключ нужен для цепочки:

```text
deploy-guest
→ создать/запустить гостя
→ дождаться management-доступа
→ SSH как ops
→ выполнить ограниченный bootstrap
→ передать дальнейшее управление provisioning-контуру
```

`deploy-guest` **не создаёт и не ротирует** эту пару. Lifecycle credential принадлежит PVE Stage 1.

`pve_guest_ed25519` нельзя переиспользовать как GitHub Deploy Key, AI Control key или будущую Ansible identity 311. Потеря private key считается recovery-ситуацией: автоматическая незаметная генерация новой пары запрещена, потому что старый public key может уже находиться в `authorized_keys` существующих гостей.

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

Пара создаётся **внутри `301`** общим platform-скриптом `prepare-ai-control.sh`, а не installer конкретного AI-агента и не PVE Stage 1.

Правила:

```text
private key
→ остаётся только внутри 301
→ не попадает в Git
→ не попадает в tpl-debian13

public key
→ может добавляться конкретным managed VM, которым требуется direct AI SSH
→ передаётся пользователю ops, а не заменяет host-side guest-bootstrap identity
```

После установки конкретного агента он может использовать:

```text
301-ai-control
→ /opt/ai-control/ssh/ai_control_ed25519
→ SSH ops@managed-guest
```

Этот direct SSH предназначен для diagnostics, one-off и emergency действий AI control plane. Первичный host-side bootstrap выполняется отдельным `pve_guest_ed25519`, а повторяемая конфигурация после появления `311-dev-services` выполняется Ansible.

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

## Ansible / 311 identity

Provisioning-контур `311-dev-services` должен иметь собственную SSH identity. Она не должна совпадать ни с host-side `pve_guest_ed25519`, ни с ключом `301-ai-control`.

Принцип:

```text
PVE / deploy-guest key
→ initial bootstrap/handoff

311 / Ansible key
→ повторяемый provisioning Linux-гостей

301 / AI Control key
→ AI diagnostics/one-off/emergency при необходимости
```

Конкретный lifecycle Ansible key фиксируется вместе с реализацией provisioning 311.

## Другие technical keys

Для независимых ролей допускаются отдельные пары, например:

```text
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

Если technical private key находится внутри рабочей VM, полный Proxmox backup этой VM содержит этот key. Поэтому backup `301-ai-control` и будущего `311-dev-services` с provisioning credentials считается чувствительным объектом.

Host-side `/etc/proxmox-deployer/ssh/pve_guest_ed25519` не входит в backup конкретной VM и должен резервироваться отдельно как PVE infrastructure secret.

## Итоговая модель

```text
9000 tpl-debian13
├── ops account
├── passwords locked
└── authorized_keys empty

          ↓ Full Clone + Cloud-Init

managed VM
└── /home/ops/.ssh/authorized_keys
    ├── PVE PVE guest public key
    ├── AI Control public key — если требуется direct AI SSH
    ├── Ansible/311 public key — после ввода provisioning-контура
    └── personal public keys — при необходимости

PVE host
└── /etc/proxmox-deployer/ssh/pve_guest_ed25519
    └── private host-side bootstrap identity

301-ai-control
├── /opt/ai-control/ssh/ai_control_ed25519
│   └── private AI infrastructure identity
└── /opt/ai-control/ssh/github_proxmox_repo_ed25519
    └── private GitHub-only identity

311-dev-services
└── отдельная private Ansible/provisioning identity
```

Таким образом private keys никогда не требуется копировать в создаваемую VM: guest получает только необходимые public halves.