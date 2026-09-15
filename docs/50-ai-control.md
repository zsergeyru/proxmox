# AI Control — архитектурные принципы

## Статус

Архитектура `301-ai-control` остаётся принятой. Переписывается только исполняемая реализация bootstrap/install/deploy scripts.

Старые public scripts перенесены в архив и больше не считаются рабочим кодом, но это **не отменяет** принятые архитектурные решения.

## Разделение ответственности

```text
Proximo MCP
→ VM/LXC lifecycle, guest-level Proxmox operations, snapshots/backups и diagnostics

host-side deploy-guest
→ создание новых VM/LXC по guest.yaml

Ansible на 311-dev-services
→ повторяемая конфигурация ОС и приложений внутри гостей через SSH

прямой SSH из 301
→ bootstrap, диагностика, разовые и аварийные действия
```

Ansible/Semaphore не размещаются в `301`; они относятся к `311-dev-services`.

## 301-ai-control

`301-ai-control` — целевая VM центрального AI-контура. `320-ai-control` остаётся текущим bootstrap/legacy control node до успешного ввода `301` и отдельного решения о выводе `320`.

Общая структура control plane сохраняется:

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

Конкретный AI-agent отделён от общих capabilities. Docker/runtime, Proximo, SSH identities и Git checkout не должны дублироваться внутри каждого агента.

## Proximo

Канонический Proxmox MCP для управляющего контура — Proximo (`proximo-proxmox`).

Hard security boundary задаётся PVE identity/token/ACL. AI Control не получает штатных прав на host network, IAM/ACL, SDN, storage definitions, certificates/repositories или reboot/shutdown самого PVE.

Основная write-zone `ai-agent@pve!infra` — resource pool `managed`. `301` не должен входить в собственную обычную self-managed write-zone.

Новые VM/LXC AI control не создаёт напрямую через права своего Proxmox token. Агент инициирует host-side `deploy-guest`, а фактическое создание/clone/configuration выполняет `deployer@pve!host-deploy` по `guest.yaml`. После успешного deploy обычный guest помещается в `managed`, и далее им может управлять AI runtime.

Каноническая политика двух PVE identities и границы `managed` описана в:

[`25-pve-access-control.md`](25-pve-access-control.md)

ADR в `guests/320-ai-control/` относится к существующему bootstrap/legacy-контру `320` и не является source of truth для прав новой схемы `301`.

## SSH

Единый административный пользователь Debian-инфраструктуры — `ops`.

Управляющий контур использует отдельные SSH identities по назначению. Private keys и provider credentials не хранятся в Git.

Public infrastructure key передаётся managed Debian VM через Cloud-Init пользователю `ops` до первого запуска.

## Git

Приватный `zsergeyru/proxmox` остаётся source of truth.

GitHub access для AI Control выполняется отдельной Deploy Key identity. Конкретные шаги создания/регистрации ключа реализуются новыми scripts, но архитектурное разделение Git credential и infrastructure SSH identity сохраняется.

## Managed Debian VM

Целевая модель остаётся:

```text
AI request / operator
→ deploy-guest <VMID>
→ Full Clone from 9000
→ CPU/RAM/disk/network по guest.yaml
→ ciuser=ops
→ SSH public key через Cloud-Init
→ managed pool, если guest не является исключением
→ first start
→ QGA/SSH/health verification
```

Template `9000` остаётся защищённым и используется host deployer как источник clone, но не входит в обычную write-zone AI runtime.

## Repeatable deploy

После развёртывания `311-dev-services` штатный repeatable flow для изменений внутри ОС:

```text
AI agent / пользователь
→ конфигурация в Git
→ Ansible на 311
→ SSH
→ нужный guest
→ health/status/log verification
```

Создание нового guest выполняется отдельно через `deploy-guest`.

Прямой SSH из `301` остаётся для bootstrap, диагностики, разовых и аварийных действий.

## Что именно переписывается с нуля

Не считаются действующей реализацией старые файлы:

```text
create-ai-control-vm.sh
prepare-ai-control.sh
install-ai-agent.sh
bootstrap-ai-control.sh
```

Они находятся в public archive только как история.

Новые scripts должны реализовать описанную здесь архитектуру и требования из:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`25-pve-access-control.md`](25-pve-access-control.md);
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md);
- актуальных ADR `311-dev-services` и `301-ai-control`.

Их названия, язык реализации и внутренняя структура могут быть другими.

## Security boundary

1. PVE privilege-separated token + ACL — hard boundary.
2. `ai-agent@pve!infra` штатно управляет гостями в `managed`.
3. Создание новых VM/LXC идёт через host-side `deploy-guest`, а не через прямой `VM.Allocate/VM.Clone` AI token.
4. MCP surface ограничивается необходимыми guest-level operations.
5. Common platform отделена от agent-specific software.
6. Private SSH keys и provider credentials не хранятся в Git.
7. Snapshots/backups используются перед рискованными изменениями.
8. `301`, production HA и template не входят автоматически в self-managed write-zone.

## Готовность к замене 320

`320-ai-control` выводится из эксплуатации только после live-проверки новой реализации `301`, включая Proximo, Git/SSH, вызов `deploy-guest`, доступ к managed test guest и взаимодействие с Ansible на `311`.
