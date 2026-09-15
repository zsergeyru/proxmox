# Пользователи, SSH-ключи и доступ к Debian VM/LXC

## `root` как единый management user

Для управляемой Debian-инфраструктуры используется один обязательный административный Linux-user:

```text
root
```

Generic user `ops` больше не является частью project contract и не создаётся template/deployer ради управления инфраструктурой.

Root password заблокирован. Штатная SSH policy:

```text
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Следовательно сетевой административный доступ выглядит так:

```text
SSH → root + разрешённый private key
```

Разные субъекты управления различаются SSH-ключами, а не Linux-user names.

## Template не содержит credentials

В `tpl-debian13` Template-Version 6:

```text
root password = locked
root authorized_keys = empty
```

Ни personal keys, ни deployer/AI/Ansible keys в template не зашиваются. Перед seal builder удаляет `/root/.ssh`.

Каждый конкретный clone получает нужные public keys отдельно до первого запуска.

## Cloud-Init порядок для VM

Для обычной Debian VM:

```text
Full Clone from 9000
→ protection=0, если guest.yaml не требует обратного
→ hostname/name
→ CPU/RAM/disk/network
→ ciuser=root
→ sshkeys=<нужные public keys>
→ cloud-init update
→ first start
→ QEMU Agent/SSH/health check
```

Private key через Cloud-Init никогда не передаётся.

## LXC initial access

Для Debian LXC отдельный bootstrap-user не создаётся.

При создании контейнера Proxmox получает файл public keys через штатный параметр `ssh-public-keys`. Ключи устанавливаются для `root` ещё при создании LXC.

Нормальный flow:

```text
resolve LXC template
→ собрать набор public keys
→ create LXC с ssh-public-keys
→ start
→ проверить root SSH
→ optional bootstrap/provisioning
```

`base` capability не участвует в получении первоначального SSH-доступа и может вообще отсутствовать.

## Host-side PVE guest identity

Private PVE Stage 1 создаёт постоянную technical identity для host-side доступа `deploy-guest` к Linux-гостям:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Правила:

```text
private key
→ остаётся на PVE
→ принадлежит host-side deployer/pvedeploy
→ не попадает в Git
→ не попадает в template

public key
→ передаётся создаваемому гостю
→ VM: root authorized_keys через Cloud-Init
→ LXC: root authorized_keys через ssh-public-keys
```

Этот keypair создаёт и восстанавливает Stage 1. `deploy-guest` не генерирует и не ротирует его.

Потеря private key считается recovery-ситуацией: автоматическая незаметная ротация запрещена, потому что старый public key уже может быть установлен в существующих гостях.

## AI Control identity

AI Control имеет отдельную SSH identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key принадлежит AI control plane и не является `pve_guest_ed25519`.

Назначение:

```text
AI agent
→ ai_control_ed25519
→ SSH root@guest
```

Public half может быть установлен в `/root/.ssh/authorized_keys` любой VM/LXC, к которой AI должен иметь прямой shell-доступ.

AI SSH policy **не выводится автоматически из membership в pool `managed`**:

```text
PVE managed ACL
→ права AI на Proxmox lifecycle/configuration

наличие ai_control_ed25519.pub в guest
→ прямой root SSH AI внутрь ОС
```

Поэтому перенос гостя в/из `managed` не требует автоматически добавлять или удалять SSH key.

Если AI SSH к конкретному guest нужно запретить, удаляется только строка `ai_control_ed25519.pub` из его `/root/.ssh/authorized_keys`. Host-side deployer и другие identities продолжают работать своими ключами.

## Как новый guest получает несколько ключей

VM и LXC могут получать несколько public keys для одного пользователя `root`.

Типичный target:

```text
/root/.ssh/authorized_keys
├── pve_guest_ed25519.pub       # host-side deployer
├── ai_control_ed25519.pub      # AI control, если нужен direct SSH
├── ansible/provisioning key    # после ввода 311
└── personal keys               # при необходимости
```

Для LXC `ssh-public-keys` принимает файл с несколькими OpenSSH public keys по одному на строку. Для VM тот же набор передаётся через Proxmox Cloud-Init `sshkeys`.

Ключи не означают отдельных Linux-пользователей: все эти identities входят как `root`.

## Источник AI public key для host-side deploy

Private AI key не должен копироваться на PVE только ради создания гостей.

AI public key является несекретным runtime credential. После создания identity в `301-ai-control` его public half должен быть зарегистрирован в host-side deploy workflow отдельно от private key. Конкретный механизм регистрации реализуется вместе с новым `301`/`deploy-guest` tooling; до этого host-side deploy обязан уметь создать guest как минимум с `pve_guest_ed25519.pub`, а отсутствие зарегистрированного AI public key не должно блокировать базовый deploy.

После регистрации `deploy-guest` может включать оба public key в initial keyset. Удаление AI key из уже существующего guest считается явным отзывом прямого AI SSH и не должно автоматически отменяться обычным reconcile PVE-параметров.

## AI-created guests

Когда guest создаёт сам AI через Proximo, AI уже имеет свой public key и может передать его в Cloud-Init/LXC create options.

Host-side public key также должен быть включён, когда он доступен AI control plane как зарегистрированный public infrastructure credential. Public keys можно безопасно передавать между управляющими контурами; private halves между ними не копируются.

## GitHub identity AI Control

Для доступа к приватному инфраструктурному Git используется **другая** пара:

```text
/opt/ai-control/ssh/github_proxmox_repo_ed25519
/opt/ai-control/ssh/github_proxmox_repo_ed25519.pub
```

Она используется только для:

```text
git@github.com:zsergeyru/proxmox.git
```

Не использовать GitHub key для SSH в инфраструктурные VM/LXC и не использовать infrastructure guest key как GitHub Deploy Key.

## Ansible / 311 identity

Provisioning-контур `311-dev-services` должен иметь собственную SSH identity, отличную от host-side и AI identities.

Принцип:

```text
PVE / deploy-guest key
→ host-side management

301 / AI Control key
→ AI direct management

311 / Ansible key
→ повторяемый provisioning
```

Все три могут авторизоваться как `root`; различие сохраняется на уровне keypair и audit/credential lifecycle.

## Personal SSH keys

Personal key pair принадлежит устройству человека. Private key хранится только на соответствующем устройстве. Public key может быть добавлен конкретному guest при необходимости.

Не требуется создавать универсального Linux-user `ops` только ради personal SSH. Если позднее понадобится отдельный человеческий аккаунт с ограниченным sudo, он создаётся как отдельное осознанное решение и не является частью generic deploy contract.

## Другие technical keys

Для независимых ролей допускаются отдельные пары, например:

```text
backup_ed25519
ci_ed25519
```

Общий принцип тот же: private key находится там, откуда инициируется действие; target получает только public half.

## SSH host keys VM

`/etc/ssh/ssh_host_*` идентифицируют сервер, а не клиента. Перед превращением builder в template они удаляются, поэтому каждый clone создаёт уникальные host keys при первом boot.

## Proxmox Console

Template-Version 6 использует:

```text
vga: std
Proxmox Web UI
→ noVNC/VGA
→ tty1
→ autologin root
```

Fallback:

```text
serial0: socket
→ ttyS0
→ autologin root
```

Поэтому право `VM.Console` является фактическим административным root-доступом к гостю и выдаётся только доверенным PVE identities.

## Backup и private keys

Если technical private key находится внутри рабочей VM, полный Proxmox backup этой VM содержит этот key. Поэтому backup `301-ai-control` и будущего `311-dev-services` с provisioning credentials считается чувствительным объектом.

Host-side `/etc/proxmox-deployer/ssh/pve_guest_ed25519` не входит в backup конкретной VM и должен резервироваться отдельно как PVE infrastructure secret.

## Итоговая модель

```text
9000 tpl-debian13 v6
├── management user root
├── root password locked
├── root SSH key-only
└── authorized_keys empty

          ↓ VM Cloud-Init / LXC ssh-public-keys

Linux guest
└── /root/.ssh/authorized_keys
    ├── PVE/deployer public key
    ├── AI Control public key — если разрешён direct AI SSH
    ├── Ansible/311 public key — при provisioning
    └── personal public keys — при необходимости

PVE host
└── /etc/proxmox-deployer/ssh/pve_guest_ed25519
    └── private host-side identity

301-ai-control
├── /opt/ai-control/ssh/ai_control_ed25519
│   └── private AI guest-management identity
└── /opt/ai-control/ssh/github_proxmox_repo_ed25519
    └── private GitHub-only identity

311-dev-services
└── отдельная private Ansible/provisioning identity
```

Главное правило: **один management user `root`, несколько независимых SSH identities. Доступ отзывается ключом, а не созданием/удалением generic admin users.**
