# AI Control — архитектурные принципы

## Статус

Архитектура `301-ai-control` остаётся принятой. Переписывается только исполняемая реализация bootstrap/install/deploy scripts.

Старые public scripts перенесены в архив и больше не считаются рабочим кодом, но это **не отменяет** принятые архитектурные решения.

Каноническая модель PVE access/deploy задаётся [`25-pve-access-control.md`](25-pve-access-control.md). При расхождении этот документ должен быть приведён к ней.

## Разделение ответственности

```text
Proximo MCP + ai-agent@pve!infra
→ штатный AI lifecycle обычных VM/LXC в разрешённой зоне managed
→ create/clone/configure/start/stop/snapshot/backup/delete и diagnostics

host-side deploy-guest + deployer@pve!host-deploy
→ человек / локальная host-side automation
→ deterministic PLAN/APPLY из guest manifests

Ansible на 311-dev-services
→ повторяемая конфигурация ОС и приложений внутри гостей через SSH

прямой SSH из 301
→ bootstrap, диагностика, разовые и аварийные действия
```

`deploy-guest` не является обязательным security gateway для AI agent. AI и человек используют разные PVE identities и оставляют раздельный audit trail.

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

Основная write-zone `ai-agent@pve!infra` — resource pool `managed`.

Штатный AI flow для нового обычного гостя:

```text
AI agent
→ читает/учитывает Git desired state
→ Proximo
→ create/clone с pool=managed
→ CPU/RAM/disk/network и guest-level options
→ start/verify
→ дальнейшее guest-level управление в managed
```

AI agent может самостоятельно создавать, клонировать, конфигурировать и удалять обычные разрешённые VM/LXC через Proximo. Для этого не требуется промежуточный вызов host-side `deploy-guest`.

Новый обычный guest должен сразу создаваться в `managed`. AI не должен сначала создавать его вне pool, а затем «усыновлять» отдельной операцией.

AI не получает права произвольно добавлять в свою write-zone существующие protected/self-managed объекты вне `managed`. По умолчанию к таким объектам относятся как минимум:

```text
100   HAOS production
301   AI control plane
320   bootstrap/legacy AI control
9000  Debian VM template
```

Template `9000` остаётся отдельным clone source: AI может использовать его как источник разрешённого clone, но не получает обычное право изменять сам template.

Каноническая политика двух PVE identities, ролей и границы `managed` описана в [`25-pve-access-control.md`](25-pve-access-control.md).

## SSH

Единый административный пользователь Debian-инфраструктуры — `ops`.

Управляющий контур использует отдельные SSH identities по назначению. Private keys и provider credentials не хранятся в Git.

Public infrastructure key передаётся managed Debian VM пользователю `ops` предусмотренным deploy/bootstrap-механизмом.

GitHub credential и infrastructure SSH identity — разные credentials и не должны использоваться взаимозаменяемо.

## Git

Приватный `zsergeyru/proxmox` остаётся source of truth.

GitHub access для AI Control выполняется отдельной Deploy Key identity. Конкретные шаги создания/регистрации ключа реализуются новыми scripts, но архитектурное разделение Git credential и infrastructure SSH identity сохраняется.

## Создание managed Debian guest

Есть два штатных пути, которые не следует смешивать.

### Человек / host-side automation

```text
оператор
→ deploy-guest <VMID> [--apply]
→ deployer@pve!host-deploy
→ Full Clone from 9000 либо создание LXC по pinned ostemplate
→ guest-level desired state из Git
→ managed pool, если guest не является исключением
→ bootstrap/verify
```

### AI automation

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ create/clone сразу в managed
→ guest-level desired state из Git
→ bootstrap/provisioning предусмотренным guest workflow
→ verify
```

`deploy-guest` остаётся удобным человеческим/direct-host инструментом, но AI не обязан вызывать его для обычных managed guests.

`301` является специальным случаем: он не должен зависеть от уже работающего собственного AI control plane для первоначального создания. Его initial creation выполняется host-side bootstrap/deploy workflow.

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

PVE lifecycle при этом остаётся за соответствующей PVE identity: AI использует Proximo, человек может использовать `deploy-guest`.

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
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md);
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md);
- актуальных ADR `311-dev-services` и `301-ai-control`.

Их названия, язык реализации и внутренняя структура могут быть другими.

## Security boundary

1. PVE privilege-separated token + ACL — hard boundary.
2. `ai-agent@pve!infra` штатно управляет обычными гостями в `managed` через Proximo.
3. AI может create/clone/delete обычные managed guests напрямую; `deploy-guest` предназначен для человека/host-side automation и не является обязательным AI gateway.
4. Новый обычный AI-created guest сразу создаётся в `managed`; произвольное принятие protected VM извне `managed` не является штатной операцией.
5. MCP surface ограничивается необходимыми guest-level operations.
6. Common platform отделена от agent-specific software.
7. Private SSH keys и provider credentials не хранятся в Git.
8. Snapshots/backups используются перед рискованными изменениями.
9. `301`, production HA, bootstrap `320` и template `9000` не входят автоматически в self-managed write-zone.

## Готовность к замене 320

`320-ai-control` выводится из эксплуатации только после live-проверки новой реализации `301`, включая:

1. host-side создание и bootstrap самого `301` без зависимости от работающего 301;
2. Git/SSH identities и common platform;
3. Proximo и реальную авторизацию `ai-agent@pve!infra`;
4. создание/clone тестового обычного guest сразу в `managed` через Proximo;
5. изменение guest-level конфигурации и lifecycle тестового guest;
6. подтверждение отсутствия обычного write-доступа к protected/self-managed объектам;
7. взаимодействие с Ansible на `311`;
8. backup/recovery и credential handling.
