# AI Control — архитектурный contract

**Type:** Specification  
**Status:** Active  
**Source of truth:** Yes — для target architecture `301-ai-control`, его management boundaries и взаимодействия Proximo/Git/SSH/Ansible.

Пошаговый ввод `301-ai-control` находится в [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md). Точные PVE permissions задаёт [`25-pve-access-control.md`](25-pve-access-control.md), credential policy — [`23-security.md`](23-security.md), initial guest access — [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 1. Разделение ответственности

```text
Proximo MCP + ai-agent@pve!infra
→ PVE lifecycle обычных VM/LXC в зоне managed
→ create/clone/configure/start/stop/snapshot/backup/delete и diagnostics

host-side deploy-guest + deployer@pve!host-deploy
→ человек / локальная host-side automation
→ deterministic PLAN/APPLY из guest manifests

Ansible на 311-dev-services
→ повторяемая конфигурация ОС и приложений внутри гостей через SSH

прямой SSH из 301
→ диагностика, bootstrap, one-off и аварийные действия внутри guest OS
```

`deploy-guest` не является обязательным security gateway для AI agent. AI и человек используют разные PVE identities.

Ansible/Semaphore не размещаются в `301`; они относятся к `311-dev-services`.

## 2. `301-ai-control`

`301-ai-control` — целевая VM центрального AI-контура.

`320-ai-control` остаётся bootstrap/legacy control node до успешного ввода `301` по runbook и отдельного решения о выводе `320`.

Целевая структура:

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   └── proximo/
├── ssh/
├── repos/
│   └── proxmox/
└── state/
```

Docker/runtime, Proximo, infrastructure SSH identities и Git checkout являются common platform и не дублируются внутри каждого агента.

## 3. Proximo и `managed`

Канонический Proxmox MCP — Proximo (`proximo-proxmox`).

Hard boundary PVE задаётся identity/token/ACL.

Основная PVE write-zone `ai-agent@pve!infra`:

```text
/pool/managed
```

Нормальный lifecycle:

```text
AI agent
→ Proximo
→ create/clone сразу с pool=managed
→ CPU/RAM/disk/network и guest-level options
→ start/verify
→ дальнейшее PVE guest-level управление
```

AI может самостоятельно создавать, клонировать, конфигурировать и удалять обычные разрешённые VM/LXC через Proximo.

Template `9000` остаётся отдельным clone source и не входит в обычную write-zone AI.

AI Control не получает штатных прав на:

- host network;
- IAM/ACL administration;
- SDN infrastructure administration;
- storage definitions;
- certificates/repositories самого PVE;
- host firewall;
- reboot/shutdown физического PVE.

Точный privilege set и ACL matrix принадлежат [`25-pve-access-control.md`](25-pve-access-control.md).

## 4. PVE access и guest SSH независимы

Pool `managed` определяет **права AI на объект Proxmox**, но не определяет содержимое `authorized_keys` внутри Linux.

```text
PVE:
guest в managed
→ ai-agent@pve!infra получает предусмотренные guest-level permissions

guest вне managed
→ эти PVE permissions не распространяются
```

Отдельно:

```text
Guest OS:
есть ai_control_ed25519.pub в /root/.ssh/authorized_keys
→ AI может SSH root внутрь ОС

ключ удалён
→ direct AI SSH запрещён
```

Перемещение VM/LXC в `managed` или из него само по себе не должно изменять `authorized_keys`.

Поэтому guest вне `managed` может сознательно оставаться доступным AI по SSH для диагностики ОС, при этом AI не получает права менять его PVE hardware/lifecycle через Proximo.

## 5. Management SSH

Единый management user управляемой Debian-инфраструктуры:

```text
root
```

Root password locked. SSH password authentication disabled. Root login разрешён только по public key.

AI Control использует отдельную infrastructure identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся в AI control plane. Public key устанавливается тем Linux guests, которым разрешён direct AI SSH.

Host-side deployer использует другой keypair `pve_guest_ed25519`; Ansible/311 — свою provisioning identity.

GitHub credential и guest-management SSH identity — разные credentials.

Полный lifecycle credentials находится в [`23-security.md`](23-security.md).

## 6. Host-side создание guest

Host-side flow по Git desired state:

```text
оператор
→ deploy-guest <VMID> [--apply]
→ deployer@pve!host-deploy
→ Full Clone current template 9000 либо LXC из Debian 13 family selector
→ установить host-side public key для root
→ при необходимости включить зарегистрированный AI public key
→ start
→ verify root SSH
→ optional bootstrap
```

Initial root SSH не зависит от `base` capability.

Сам `301` является специальным случаем: его initial creation выполняется host-side и не зависит от уже работающего AI Control.

## 7. AI-created guest

AI lifecycle:

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ create/clone сразу в managed
→ передать AI public key через Cloud-Init/LXC create options
→ при доступном host-side public credential добавить также deployer public key
→ start
→ verify
```

AI не обязан вызывать host-side `deploy-guest`.

Public infrastructure keys можно передавать между control planes; private halves не копируются.

## 8. Repeatable provisioning

После развёртывания `311-dev-services` повторяемая конфигурация Linux выполняется через отдельный Ansible credential:

```text
AI agent / пользователь
→ конфигурация в Git
→ Ansible на 311
→ SSH root по provisioning key
→ target guest
→ health/status/log verification
```

Это не запрещает прямой AI SSH для one-off/diagnostic действий.

## 9. Git

Private `zsergeyru/proxmox` остаётся source of truth для project configuration.

AI Control имеет собственный Git credential и отдельный checkout:

```text
/opt/ai-control/repos/proxmox
```

Git credential не используется как guest SSH credential.

AI может подготавливать project changes через Git/GitHub, но это не даёт ему права изменять root-trusted canonical checkout на самом PVE.

## 10. Security invariants

1. PVE token + ACL определяют PVE operations AI.
2. `ai-agent@pve!infra` штатно управляет обычными гостями только в `managed`.
3. Direct guest SSH является отдельным credential boundary.
4. AI SSH key не выводится автоматически из pool membership.
5. AI может create/clone/delete обычные managed guests напрямую через Proximo.
6. MCP surface ограничивается guest-level operations, необходимых проекту.
7. Private SSH keys, tokens и provider credentials не хранятся в Git.
8. Root password authentication отключена.
9. `301`, production HA, legacy `320` и template `9000` не входят автоматически в AI write-zone `managed`.
10. Ansible provisioning использует отдельную identity.

## 11. Связанные документы

- [`25-pve-access-control.md`](25-pve-access-control.md) — точные PVE roles/privileges/ACL;
- [`23-security.md`](23-security.md) — SSH identities и credential lifecycle;
- [`30-guest-manifest.md`](30-guest-manifest.md) — guest desired state;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — initial management access и provisioning boundary;
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) — Runbook создания/ввода/acceptance test `301`;
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — template `9000` contract.

Главный contract: **`301-ai-control` предоставляет AI common platform, Proximo и независимый SSH/Git access; PVE lifecycle AI ограничен `managed`, а guest SSH и provisioning остаются отдельными boundaries.**
