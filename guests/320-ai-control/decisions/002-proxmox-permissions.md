# ADR 002 — Права AI control agents в Proxmox

## Статус

Принято для bootstrap `320-ai-control` и как базовая модель для будущего `301-ai-control`.

## Контекст

`ai-control` является мультиагентным контуром. Сейчас в `/opt/ai-control/agents/` предусмотрены как минимум `hermes/` и `agent-zero/`, а в будущем могут появляться другие агенты.

Поэтому права на Proxmox не привязываются архитектурно к Hermes как к единственному агенту. Они принадлежат отдельной management identity управляющего контура и могут использоваться теми агентами, которым разрешён доступ к общему Proximo MCP.

Конфигурация и файлы конкретного агента остаются в его каталоге `agents/<name>/`. Общие MCP размещаются отдельно в `/opt/ai-control/mcp/` и не дублируются по каталогам агентов.

## Решение

AI control agents получают административные права над жизненным циклом выделенных VM/LXC, но **не получают административные права над самим Proxmox VE host**.

Права выдаются через отдельного Proxmox API user/token с privilege separation и ACL на выделенный resource pool управляемых гостей. Не использовать `root@pam`, `PVEAdmin` или ACL на весь `/` без отдельного решения.

Конкретный агент не должен получать отдельный более мощный Proxmox token только потому, что ему не хватает функции в MCP. Расширение прав выполняется отдельным инфраструктурным решением.

## Матрица прав

| Область | AI control agents | Правило |
|---|---|---|
| Просмотр PVE node, ресурсов и tasks | разрешено | read/audit |
| Просмотр VM/LXC config/status | разрешено | read/audit |
| Создание VM/LXC | разрешено | только в managed pool |
| Clone из template | разрешено | основной способ развёртывания |
| Изменение CPU/RAM | разрешено | только managed guests |
| Изменение guest disks | разрешено | add/resize/remove только managed guests |
| Изменение guest NIC | разрешено | только конфигурация NIC гостя; не host network |
| Start/Stop/Reboot VM | разрешено | только managed guests |
| Start/Stop/Reboot LXC | разрешено | только managed guests |
| Guest shutdown | разрешено | штатное управление гостем |
| Snapshots | разрешено | create/list/delete для managed guests |
| Snapshot rollback | разрешено | для восстановления managed guests |
| Backup VM/LXC | разрешено | запуск backup управляемого гостя |
| Restore backup | ограниченно | предпочтительно в новый/свободный VMID; production не перезаписывать автоматически |
| Delete VM/LXC | разрешено с защитными правилами | только managed pool; перед удалением проверить назначение и состояние backup |
| Console/guest diagnostics | разрешено | для managed guests |
| QEMU Guest Agent operations | разрешено | status/shutdown/network info и другие guest-level операции |
| Migration между PVE nodes | не требуется | сейчас один node; отдельное решение при появлении кластера |
| Использование существующего storage | разрешено | только для дисков/clone/backup в рамках гостевых операций |
| Изменение storage configuration | запрещено | нельзя добавлять/удалять/перенастраивать PVE storage |
| Изменение PVE host network | запрещено | bridge, physical NIC, routes, `/etc/network/interfaces` — только вручную |
| Reboot/Shutdown PVE host | запрещено | управление физическим гипервизором остаётся у человека |
| Shell на PVE host через API | запрещено | не требуется для штатной работы агентов |
| Datacenter/host firewall | запрещено | отдельное административное решение |
| SDN/VLAN infrastructure | запрещено | агент меняет только NIC конкретного гостя |
| HA configuration | запрещено | отдельное решение при необходимости |
| Users/groups/realms | запрещено | IAM остаётся вне зоны агента |
| ACL/roles/API tokens | запрещено | агент не может расширять собственные права |
| Certificates/ACME | запрещено | host-level administration |
| PVE repositories/update host | запрещено | host-level administration |

## Managed pool

Для write-доступа AI control используется отдельный resource pool, например:

```text
managed
```

Планируемая write-зона:

```text
109 network-gateway
201 ha-main
202 ha-test
203 ha-flat2
211 automation-services
301 ai-control
311 dev-services
321 app-services
331 ai-services
401 monitoring
501 frigate (после выбора VM/LXC)
```

Фактическое помещение гостя в managed pool выполняется только тогда, когда управляющему контуру действительно разрешено им управлять.

## Защищённые объекты

### VM 100 — HAOS production

На первом этапе остаётся вне write-зоны AI control. Разрешён audit/read. Изменения production Home Assistant через Proxmox выполняются человеком до отдельного решения.

### VM 320 — bootstrap ai-control

На первом этапе остаётся вне write-зоны агентов, работающих внутри самой `320`. Агент не должен иметь возможность случайно остановить, удалить или перенастроить VM, внутри которой работает управляющий bootstrap-контур.

После перехода на `301-ai-control` права на бывший bootstrap `320` определяются отдельным решением перед выводом из эксплуатации.

### Template 9000 — tpl-debian13

AI control должен иметь возможность видеть template и использовать его как источник clone, но не изменять и не удалять сам template. Сборка и обновление `9000` выполняются отдельно.

## Предполагаемые Proxmox privileges

Точная команда `pveum` должна проверяться по фактической версии PVE и требованиям выбранного Proximo MCP. Базово custom role должна покрывать guest lifecycle и не включать host administration.

Ожидаемая группа privileges:

```text
VM.Audit
VM.Allocate
VM.Clone
VM.PowerMgmt
VM.Config.CPU
VM.Config.Memory
VM.Config.Disk
VM.Config.Network
VM.Config.Options
VM.Snapshot
VM.Snapshot.Rollback
VM.Backup
Datastore.Audit
Datastore.AllocateSpace
```

При необходимости добавляются только точечные guest-level privileges, требуемые для Cloud-Init, console или конкретной операции MCP.

Не выдавать management identity privileges административного уровня, позволяющие менять host/IAM/ACL/storage configuration/SDN или расширять собственные полномочия.

## Правила безопасного destructive action

Разрешение API не означает, что destructive operation выполняется без проверки. Для удаления, rollback и restore действуют правила:

1. определить целевой VMID/LXC и убедиться, что он входит в managed pool;
2. проверить, не является ли объект production/защищённым;
3. перед рискованным изменением создать snapshot, если операция и тип гостя это допускают;
4. для удаления проверить наличие актуального backup либо явно зафиксированную воспроизводимость;
5. сначала создать и проверить замену, затем удалять старый объект;
6. после действия проверить status/health/logs;
7. не изменять ACL или права собственного token ради выполнения задачи.

## Разделение Proxmox / guest OS

Эта модель относится только к уровню виртуализации.

```text
общий Proximo MCP
→ lifecycle VM/LXC

Ansible на 311-dev-services
→ повторяемые изменения внутри ОС

прямой SSH из ai-control
→ bootstrap, диагностика, аварийные и разовые действия
```

Полные права внутри конкретных управляемых Linux-гостей могут предоставляться через отдельного management-пользователя и `sudo`, но это не даёт AI agents дополнительных прав на PVE host.

## Главный принцип

> AI control agents администрируют выделенные гости, но не администрируют гипервизор, систему прав Proxmox или базовую инфраструктуру PVE host.
