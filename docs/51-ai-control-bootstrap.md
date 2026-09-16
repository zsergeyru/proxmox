# Bootstrap `301-ai-control`

**Type:** Runbook  
**Status:** Active  
**Source of truth:** Yes — для последовательности создания, ввода и проверки `301-ai-control`.

Этот документ отвечает на вопрос **«как ввести `301-ai-control` в работу»**. Архитектура и границы ответственности принадлежат [`50-ai-control.md`](50-ai-control.md), credential policy — [`23-security.md`](23-security.md), PVE permissions — [`25-pve-access-control.md`](25-pve-access-control.md), initial guest access — [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 1. Предварительные условия

Перед созданием `301` должны быть готовы:

- успешно настроенный PVE host;
- current template `9000`, соответствующий contract;
- host-side `pve_guest_ed25519`;
- `deployer@pve!host-deploy` для host-side deployment;
- `ai-agent@pve!infra` и ACL model, подготовленные PVE Configuration;
- private project repository доступен host-side;
- manifest `guests/301-ai-control/guest.yaml` соответствует текущему guest contract.

`301` не должен быть необходим для собственного первоначального создания.

## 2. Создание VM 301

Первоначальный lifecycle выполняется host-side:

```text
operator / deploy-guest
→ deployer@pve!host-deploy
→ Full Clone current template 9000
→ применить guest desired state
→ передать host-side public SSH key через Cloud-Init
→ start
→ дождаться QGA/Cloud-Init
→ verify root SSH
```

Management user:

```text
root
```

Password authentication запрещена; initial access выполняется по `pve_guest_ed25519`.

## 3. Проверка initial readiness

До установки AI platform подтвердить:

```text
VM запущена
QEMU Guest Agent доступен
Cloud-Init завершён
root SSH по host-side key работает
hostname/network соответствуют desired state
нет неожиданного password SSH
```

Optional bootstrap capabilities запускаются только после работающего management SSH.

## 4. Подготовка common AI platform

Внутри 301 создать базовую структуру:

```text
/opt/ai-control/
├── agents/
├── mcp/
│   └── proximo/
├── ssh/
├── repos/
└── state/
```

Common platform должна подготовить необходимые runtime dependencies, Git/OpenSSH и container/runtime tooling согласно реализации проекта.

Agent-specific software не должен владеть common Proximo, infrastructure SSH identity или project checkout как отдельными дубликатами.

## 5. Создание AI guest-management SSH identity

Создать отдельную пару:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Правила:

```text
private key
→ остаётся внутри 301
→ не копируется на PVE
→ не хранится в Git

public key
→ может передаваться Linux guests, которым разрешён direct AI SSH
```

Не использовать этот keypair как GitHub credential.

## 6. GitHub identity

Для private infrastructure repository использовать отдельный Git credential/keypair.

Цель:

```text
git@github.com:zsergeyru/proxmox.git
```

После настройки:

```text
clone/fetch project
→ /opt/ai-control/repos/proxmox
→ verify expected origin
→ verify clean checkout
```

GitHub identity и guest-management SSH identity не взаимозаменяемы.

## 7. Настройка Proximo

Установить/настроить Proximo как общий MCP к Proxmox.

Credential:

```text
ai-agent@pve!infra
```

После настройки проверить минимум:

```text
API authentication
read/status permitted objects
clone access к template 9000
write access в /pool/managed
отсутствие ожидаемо запрещённых host-level permissions
```

Точная ACL matrix определяется [`25-pve-access-control.md`](25-pve-access-control.md).

## 8. Установка AI agent

Agent-specific runtime размещается в:

```text
/opt/ai-control/agents/<agent>/
```

Hermes или другой agent должен использовать common platform:

```text
common Proximo
common project checkout
common AI guest-management SSH identity
```

Не генерировать отдельную infrastructure keypair на каждый agent без отдельного решения.

## 9. Проверка PVE lifecycle через test guest

После ввода Proximo выполнить контролируемый live-test:

```text
AI agent
→ Proximo
→ clone/create test guest
→ сразу pool=managed
→ CPU/RAM/disk/network config
→ передать AI public key
→ start
→ verify
```

Проверить:

1. test guest действительно создан в `managed`;
2. AI может выполнять разрешённые guest-level PVE operations;
3. template `9000` не стал managed guest;
4. AI не получил широкого host-level доступа;
5. direct root SSH по `ai_control_ed25519` работает, если key был передан.

## 10. Проверка независимости PVE ACL и SSH

Отдельно подтвердить два независимых механизма:

```text
managed membership
→ PVE lifecycle/config permissions

ai_control_ed25519.pub в authorized_keys
→ direct SSH внутрь guest OS
```

Тест:

1. удалить AI public key из test guest;
2. убедиться, что direct AI SSH перестал работать;
3. убедиться, что host-side SSH продолжает работать;
4. убедиться, что PVE permissions в `managed` не изменились;
5. переместить test guest в/из `managed` только в безопасной тестовой процедуре и подтвердить, что это само по себе не редактирует `authorized_keys`.

## 11. Handoff к Ansible / 311

После готовности `311-dev-services` проверить, что provisioning использует отдельную SSH identity.

```text
301 AI / operator
→ Git desired configuration
→ Ansible на 311
→ root SSH отдельным provisioning key
→ target guest
```

AI direct SSH и Ansible SSH остаются независимыми.

## 12. Backup и recovery

До признания 301 production-ready:

- включить VM backup согласно [`22-storage-and-backup.md`](22-storage-and-backup.md);
- учитывать, что backup 301 содержит чувствительные private credentials;
- проверить способ восстановления Git credential, AI SSH identity и Proximo configuration;
- выполнить restore-test согласно [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md).

## 13. Acceptance checklist

`301-ai-control` готов к замене legacy `320`, когда подтверждено:

```text
301 создаётся host-side без зависимости от работающего 301
QGA + Cloud-Init healthy
host-side root SSH работает
AI guest-management SSH identity отдельная
GitHub identity отдельная
project checkout работает
Proximo authenticates as ai-agent@pve!infra
AI создаёт test guest сразу в managed
AI выполняет разрешённые lifecycle operations
AI direct SSH работает своим ключом
удаление AI key отзывает только AI SSH
managed membership не синхронизирует authorized_keys
Ansible/311 использует отдельную identity
301 не получает обычную managed write-zone на собственную VM
backup/recovery проверены
```

До прохождения checklist `320-ai-control` не удаляется.

## 14. Failure / rollback

Если bootstrap 301 остановился:

```text
не удалять 320
не ротировать working credentials без необходимости
сохранить logs/state
определить failed layer: guest / SSH / Git / Proximo / agent
исправить prerequisite
повторить только безопасный idempotent stage
```

Если live-test оставил test guest, сначала определить его происхождение и состояние. Не удалять неизвестный VM/LXC только по VMID без проверки.

Главное правило: **runbook описывает последовательность ввода 301; архитектурные решения и permission contracts остаются в профильных Specification/Policy документах.**