# PVE access control

## Статус

Это каноническая политика прав для новой схемы PVE bootstrap/deploy и `301-ai-control`.

Она относится к двум актуальным service identities:

```text
deployer@pve!host-deploy
ai-agent@pve!infra
```

ADR из `guests/320-ai-control/` описывает существующий bootstrap/legacy-контур `320` и не является source of truth для новой схемы прав.

Главный принцип:

```text
deployer@pve!host-deploy
→ создаёт и разворачивает VM/LXC по guest.yaml

ai-agent@pve!infra
→ управляет уже разрешёнными гостями в pool managed
→ новые гости создаются только через deploy-guest
```

---

## 1. Host deployer

```text
user:  deployer@pve
token: deployer@pve!host-deploy
```

Актор — host-side команда `deploy-guest`.

Назначение:

- читать manifest `guests/*/guest.yaml`;
- создавать VM/LXC;
- clone из template `9000`;
- задавать CPU/RAM/disks/network/Cloud-Init и другие guest-level параметры;
- использовать разрешённые storage и bridge;
- запускать/останавливать guest во время deploy и verification;
- помещать обычный guest в pool `managed`;
- выполнять проверки после deploy.

`deployer@pve!host-deploy` не предназначен для администрирования самого PVE host: IAM/ACL, host network, storage definitions, repositories, certificates и reboot/shutdown физического PVE остаются вне его штатной зоны.

---

## 2. AI infrastructure identity

```text
user:  ai-agent@pve
token: ai-agent@pve!infra
```

Основная write-zone этой identity:

```text
/pool/managed
```

Внутри `managed` AI control получает обычные guest-level возможности:

- читать config/status;
- менять CPU/RAM/disks/NIC и guest options;
- start/stop/reboot/shutdown;
- snapshots и rollback;
- backup;
- console и diagnostics;
- QEMU Guest Agent operations, необходимые для диагностики и управления гостем;
- удаление managed guest с защитными проверками automation layer.

### Создание новых VM/LXC

`ai-agent@pve!infra` не получает прямые права на произвольное создание/clone новых VM/LXC и помещение их в pools.

Новый guest создаётся так:

```text
AI agent
→ запрос deploy guest <VMID>
→ host-side deploy-guest
→ deployer@pve!host-deploy
→ guest.yaml
→ create/clone/configure
→ pool managed, если manifest не задаёт исключение
→ verification
```

Таким образом агент может инициировать создание новой VM/LXC, но фактический deploy выполняется детерминированным host-side механизмом по source of truth из Git.

Способ безопасного вызова `deploy-guest` из AI control является отдельной implementation detail. Он не должен требовать выдачи `ai-agent@pve!infra` прямого `VM.Allocate`/`VM.Clone` только ради создания гостей.

---

## 3. Pool `managed`

`managed` — основная security и organization boundary для runtime automation.

```text
внутри managed
→ ai-agent может штатно управлять гостем

вне managed
→ write-доступ AI control не предполагается
```

Обычный `deploy-guest` помещает новый guest в `managed`, если manifest/документация не определяют его как защищённое исключение.

По умолчанию не включать в обычную AI write-zone:

```text
100   production HAOS
301   AI control plane
320   bootstrap/legacy AI control
9000  protected template
```

Template `9000` используется host deployer как источник clone и не управляется AI runtime напрямую.

---

## 4. Разделение ответственности

```text
deploy-guest + deployer@pve!host-deploy
→ desired-state deployment
→ создание нового guest
→ первичная конфигурация
→ добавление в managed

Proximo + ai-agent@pve!infra
→ runtime lifecycle managed guests
→ изменения guest resources
→ snapshots/backups
→ diagnostics
```

Это разделение сохраняется независимо от конкретного AI-агента или MCP implementation.

---

## 5. Роли Proxmox

Реализация должна использовать две отдельные custom roles, например:

```text
PVEHostDeployer
PVEAIManaged
```

`PVEHostDeployer` получает guest-level privileges, необходимые `deploy-guest` для create/clone/configure/start/verify и работы с разрешёнными storage/network resources.

`PVEAIManaged` получает широкий guest-level runtime набор, но ACL применяется к `managed`, а права прямого создания/clone новых объектов не выдаются.

Точные privilege names и команды `pveum` проверяются по установленной версии Proxmox VE перед реализацией, без изменения описанной здесь модели доступа.

---

## 6. Запрещённая зона для AI identity

`ai-agent@pve!infra` штатно не получает права на:

- PVE users/groups/realms;
- ACL/roles/API tokens;
- host network/bridges/routes;
- изменение storage definitions;
- SDN infrastructure administration;
- repositories/update policy;
- certificates/ACME;
- firewall самого PVE;
- reboot/shutdown физического PVE host;
- изменение собственного уровня доступа.

## Итог

```text
CREATE
AI request → deploy-guest → host-deploy token → guest.yaml → managed

RUNTIME
AI agent → ai-agent@pve!infra → /pool/managed
```

Основная защита runtime AI — граница `managed`; основная защита создания VM/LXC — обязательный проход через `deploy-guest` и `guest.yaml`.
