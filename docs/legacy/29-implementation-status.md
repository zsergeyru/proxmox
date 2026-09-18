# Статус реализации ключевых контрактов

**Тип:** Справочник  
**Статус:** Действующий  
**Основной источник:** Да — для текущего статуса реализации уже принятых архитектурных и машинных контрактов.

Этот документ не является roadmap и не задаёт сроки или приоритеты. Он отвечает только на вопрос: **что из уже принятого контракта сейчас реально реализовано в коде, а что пока существует только как спецификация будущей реализации**.

## Как читать статусы

Служебное поле `Статус` в начале остальных документов относится к жизненному циклу самого документа:

```text
Действующий
→ документ является актуальным нормативным источником проекта

Черновик
→ текст ещё не принят как окончательный контракт

Исторический
→ документ сохранён только как история и не задаёт текущее состояние
```

`Статус: Действующий` **не означает автоматически**, что весь описанный в документе код уже написан. Для этого используется таблица ниже.

## Текущее состояние

| Контракт / компонент | Статус реализации | Что это означает сейчас |
|---|---|---|
| Public Bootstrap | Реализовано | используется один постоянный root-only GitHub Deploy Key; временный GitHub key больше не создаётся |
| PVE Configuration | Реализовано | действующий модульный `configure-pve.sh`; текущая версия определяется кодом PVE Configuration |
| Guest manifest schema v7 | Реализовано | `guest.yaml` и effective state поддерживают `management.ssh_identity` и `management.project_repo_read` |
| Правило individual-only management flags | Реализовано | `ssh_identity` и `project_repo_read` разрешены только в конкретном `guest.yaml`, не наследуются из defaults/profile и нормализуются в effective state |
| `301-ai-control` management desired state | Зафиксировано в manifest | manifest уже содержит `ssh_identity: true` и `project_repo_read: true`; runtime-применение ждёт `deploy-guest` |
| `311-dev-services` management desired state | Зафиксировано в manifest | manifest уже содержит `ssh_identity: true` и `project_repo_read: true`; runtime-применение ждёт `deploy-guest` |
| `deploy-guest` | Реализовано | `scripts/pve/deploy-guest.py` реализует read-only PLAN по умолчанию и `--apply` для VM/LXC через PVE REST API с ownership, SSH trust, management handlers, Bootstrap и final verify |
| `management.ssh_identity` runtime handler | Реализовано | guest-local Ed25519 keypair создаётся только при полном отсутствии, private остаётся в guest, public регистрируется как `<VMID>.pub`, fingerprint conflict блокирует deploy, registry change запускает sync |
| `management.project_repo_read` runtime handler | Реализовано | root-wrapper передаёт fixed read key только через FD; runtime materialize/verify/remove выполняет fixed credential/known_hosts/SSH alias и точный URL rewrite для `zsergeyru/proxmox` |
| PVE public-key registry runtime | Реализовано | PVE Configuration создаёт и проверяет `/var/lib/proxmox-deployer/public-keys/`, `deployer.pub`, `<VMID>.pub` и детерминированный `management-authorized-keys` |
| `sync-management-keys` | Реализовано | отдельная PVE-команда валидирует registry, обнаруживает tagged VM/LXC, проверяет SSH trust/Debian, синхронизирует guest public catalog и только managed block `authorized_keys`; offline-гости не запускаются |
| Guest Bootstrap runtime handlers | Реализовано v1 | `base`, `git`, `docker`, `ansible_controller` применяются только после verified root SSH, с read-only check → apply → final verify и без произвольного shell/package interface |
| Расширенные CI-проверки management/runtime key contract | Реализовано для v1 | registry, sync, deploy-guest PLAN/safety helpers, wrapper trust boundary и source-revision/FD/strict-SSH invariants покрыты contract/unit checks |

## Правило обновления

После реализации очередного компонента его строка в этой таблице обновляется в том же изменении, которое вводит рабочий код или завершает соответствующую приёмочную проверку.

Если документация и код расходятся, для **требуемого поведения** основным источником остаётся профильная спецификация, а эта таблица показывает только текущую степень реализации.
