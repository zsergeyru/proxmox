# Безопасность инфраструктуры

**Type:** Policy  
**Status:** Active  
**Source of truth:** Yes — для общих security rules, management SSH model и lifecycle технических SSH identities.

Частные ограничения отдельных VM/LXC и сервисов описываются в их собственной документации. Точные Proxmox roles/privileges/ACL задаёт [`25-pve-access-control.md`](25-pve-access-control.md).

## 1. Основные правила

1. Автоматизация управляет только теми VM/LXC, для которых это явно разрешено соответствующим механизмом доступа.

2. Сам Proxmox не изменяется guest automation без отдельного решения. Особенно это относится к:
   - host network и physical NIC;
   - маршрутам самого PVE;
   - storage definitions;
   - users, roles, ACL и API tokens;
   - repositories/update policy и certificates;
   - firewall самого PVE;
   - reboot/shutdown физического host.

3. Для управляемых Debian VM/LXC единый management user — `root`. Пароль root заблокирован; password и keyboard-interactive authentication отключены. Root SSH допускается только по public key.

4. Разные субъекты управления используют разные SSH keypairs, а не разные generic Linux-users.

5. PVE ACL и guest SSH решают разные задачи. Pool `managed` ограничивает Proxmox guest-level операции AI через PVE API; наличие AI public key определяет прямой SSH внутрь конкретного guest. Эти механизмы не синхронизируются автоматически.

6. Passwords, tokens, private keys и другие secrets не хранятся в Git. Public SSH keys секретами не являются, но их происхождение и lifecycle должны быть явными.

7. Перед destructive operation проверяются правильность target и наличие способа восстановления: backup, snapshot либо воспроизводимая configuration.

8. После изменения проверяются фактическое состояние guest, нужные services и ошибки.

9. Действия automation по возможности журналируются без secret values.

10. Для LXC используются минимально необходимые privileges. Если workload требует широких system privileges или сложного hardware access, предпочтительнее VM. Docker/LXC policy: [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md).

11. Если automation не хватает прав, она не расширяет их самостоятельно. Изменение доступа является отдельным infrastructure decision.

## 2. Management SSH model

Для Debian-гостей принят единый contract:

```text
user: root
port: 22
root password: locked
PermitRootLogin: prohibit-password
PasswordAuthentication: no
KbdInteractiveAuthentication: no
PermitEmptyPasswords: no
PubkeyAuthentication: yes
```

Generic management user `ops` не является частью project contract.

Разделение субъектов управления происходит по keypair:

```text
PVE host-side deployer key
AI Control key
Ansible/provisioning key
personal key — при необходимости
other dedicated technical keys — при необходимости
```

Несколько identities могут одновременно входить как `root`, но каждая использует собственный private key. Отзыв одного public key не должен ломать остальные management channels.

## 3. Host-side PVE guest identity

Постоянная technical identity host-side deployer:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Правила:

```text
private key
→ остаётся на PVE
→ доступен только host-side deployment runtime по необходимым правам
→ не хранится в Git
→ не попадает в template

public key
→ устанавливается в guest для root SSH
→ VM: через Cloud-Init sshkeys
→ LXC: через ssh-public-keys
```

Keypair создаётся инфраструктурным bootstrap/configuration слоем, а не `deploy-guest.py`.

Обычный deploy не генерирует и не ротирует этот keypair. Потеря private key при уже разложенном public key считается recovery-ситуацией; silent rotation запрещена.

## 4. AI Control SSH identity

AI Control использует отдельную guest-management identity, например:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Назначение:

```text
AI agent
→ ai_control_ed25519
→ SSH root@guest
```

Наличие AI public key внутри guest независимо от PVE pool membership:

```text
managed ACL
→ право AI управлять lifecycle/configuration через Proxmox API

ai_control_ed25519.pub в authorized_keys
→ право AI на direct root SSH внутрь ОС
```

Перемещение guest в/из `managed` само по себе не добавляет и не удаляет AI SSH key.

Если direct AI SSH нужно отозвать, удаляется только AI public key. Host-side deployer, Ansible и другие identities продолжают использовать свои credentials.

## 5. GitHub identity AI Control

Git-доступ AI Control использует отдельный keypair, например:

```text
/opt/ai-control/ssh/github_proxmox_repo_ed25519
/opt/ai-control/ssh/github_proxmox_repo_ed25519.pub
```

Этот key используется только для Git repository access.

Нельзя:

```text
GitHub key → использовать как guest management SSH key
guest management key → использовать как GitHub Deploy Key
```

Разные trust domains должны оставаться разными credentials.

## 6. Ansible / provisioning identity

`311-dev-services` должен иметь собственную provisioning SSH identity, отличную от PVE и AI Control.

Модель:

```text
PVE key     → host-side deploy/verification
AI key      → AI direct management
Ansible key → repeatable provisioning
```

Все они могут входить как `root`, но имеют независимые private keys, audit trail и rotation lifecycle.

## 7. Personal и другие technical keys

Personal private key хранится только на устройстве человека. В guest при необходимости добавляется только public half.

Для независимых технических ролей допустимы отдельные пары, например:

```text
backup_ed25519
ci_ed25519
```

Правило одинаково для всех identities:

> private key находится там, откуда инициируется действие; target получает только public key.

## 8. Guest `authorized_keys`

Один Debian guest может содержать несколько независимых public keys пользователя `root`:

```text
/root/.ssh/authorized_keys
├── pve_guest_ed25519.pub
├── ai_control_ed25519.pub      # если разрешён direct AI SSH
├── ansible/provisioning key    # если guest управляется 311
└── personal/other keys         # при необходимости
```

Initial injection для VM/LXC и граница deployer ↔ provisioning описаны в [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 9. Template credentials policy

Template `9000` не содержит management credentials:

```text
root password = locked
/root/.ssh absent before seal
management authorized_keys = empty
private keys = absent
```

Каждый clone получает нужные public keys отдельно до первого start.

SSH host keys (`/etc/ssh/ssh_host_*`) идентифицируют сервер, а не клиента. Они удаляются перед seal template, чтобы каждый clone сгенерировал собственные уникальные host keys.

Подробный template contract: [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## 10. Console access

Proxmox Console для Debian template приводит к локальной root console. Поэтому privilege `VM.Console` является фактическим административным доступом внутрь guest и выдаётся только доверенным PVE identities.

Точная PVE ACL policy находится в [`25-pve-access-control.md`](25-pve-access-control.md).

## 11. Backup и private keys

Если private technical key находится внутри рабочей VM, полный Proxmox backup этой VM содержит этот credential и должен считаться чувствительным объектом.

Например, backups AI Control и provisioning/control-node VM/LXC требуют соответствующей защиты.

Host-side key:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
```

не входит в backup отдельного guest и должен резервироваться как отдельный PVE infrastructure secret.

## 12. Главный принцип

> Proxmox access, guest SSH и application permissions — независимые уровни. Для Debian management используется `root` только по public key, а каждый субъект управления имеет собственную SSH identity, которую можно независимо выдать, отозвать, ротировать и аудитировать.
