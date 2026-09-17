# Ввод в работу `301-ai-control`

**Тип:** Инструкция  
**Статус:** Действующий  
**Основной источник:** Да — для последовательности создания, ввода и проверки `301-ai-control`.

Архитектура и границы ответственности принадлежат [`50-ai-control.md`](50-ai-control.md), общие правила безопасности — [`23-security.md`](23-security.md), management SSH keys — [`28-management-ssh-keys.md`](28-management-ssh-keys.md), права PVE — [`25-pve-access-control.md`](25-pve-access-control.md), начальный доступ и Bootstrap — [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 1. Предварительные условия

Перед созданием `301` должны быть готовы:

- успешно настроенный PVE-хост;
- текущий шаблон `9000`;
- SSH identity deployer `pve_guest_ed25519`;
- канонический public-key registry `/var/lib/proxmox-deployer/public-keys/` как минимум с `deployer.pub`;
- `deployer@pve!host-deploy` для развёртывания со стороны PVE;
- `ai-agent@pve!infra` и ACL, подготовленные PVE Configuration;
- закрытый репозиторий проекта доступен со стороны PVE через общий GitHub Deploy Key только для чтения;
- манифест `guests/301-ai-control/guest.yaml` соответствует текущей спецификации.

`301` не должен быть необходим для собственного первоначального создания.

Для целевой модели после реализации schemas/resolver 301 использует единый management-раздел:

```yaml
management:
  ssh_identity: true
  project_repo_read: true
```

Базовый `management.ssh.user/port` приходит из общих defaults. `management.ssh_identity` и `management.project_repo_read` задаются только конкретному гостю и не наследуются.

На момент фиксации документа два целевых поля ещё не реализованы в действующей schema v6 и до реализации не добавляются в рабочий manifest.

## 2. Создание VM 301

Первоначальный жизненный цикл выполняется со стороны PVE:

```text
оператор / deploy-guest
→ deployer@pve!host-deploy
→ полный клон текущего шаблона 9000
→ применить требуемое состояние
→ передать текущий management-authorized-keys через Cloud-Init
→ установить PVE tag management-ssh
→ запустить
→ дождаться QGA/Cloud-Init
→ для ожидаемого адреса один раз сохранить SSH host key новой VM
→ повторить подключение уже со строгой проверкой сохранённого ключа
→ проверить SSH root private key deployer
→ при management.ssh_identity=true создать/проверить собственную management keypair 301
→ зарегистрировать только .pub как /var/lib/proxmox-deployer/public-keys/301.pub
→ sync-management-keys
→ при management.project_repo_read=true материализовать общий Git READ credential
```

Административный пользователь:

```text
root
```

Вход по паролю запрещён.

Tag `management-ssh` — технический runtime-маркер участия в `sync-management-keys`, а не поле `guest.yaml` и не признак membership в pool `managed`.

## 3. Проверка начальной готовности

До установки AI-платформы подтвердить:

```text
VM запущена
QEMU Guest Agent доступен
Cloud-Init завершён
PVE tag management-ssh установлен
SSH host key сохранён для ожидаемого адреса
SSH root private key deployer работает со строгой проверкой host key
имя хоста и сеть соответствуют требуемому состоянию
вход по SSH с паролем неожиданно не включён
```

После реализации `management.ssh_identity` дополнительно проверить:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

и убедиться, что private key существует только внутри 301, а соответствующая `.pub` зарегистрирована на PVE как:

```text
/var/lib/proxmox-deployer/public-keys/301.pub
```

После успешного `sync-management-keys` в 301 должна быть актуальная локальная копия:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

Если `management.project_repo_read` уже реализован и запрошен, дополнительно подтвердить наличие Git credential в стандартном месте и его корректные права.

## 4. Подготовка общей AI-платформы

Внутри `301` создать базовую структуру:

```text
/opt/ai-control/
├── agents/
├── mcp/
│   └── proximo/
├── repos/
└── state/
```

Общая платформа должна подготовить необходимые зависимости, Git/OpenSSH и средства запуска контейнеров согласно реализации проекта.

Программное обеспечение конкретного агента не должно генерировать отдельную инфраструктурную management keypair для каждого агента. Общая management identity 301 находится в стандартном месте гостя:

```text
/etc/proxmox-guest/ssh/management_ed25519
```

Общий Git read-key также не хранится как специальный AI key в `/opt/ai-control`; его управляемая копия находится в:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

## 5. Management SSH identity AI

Отдельный ручной шаг «создать `/opt/ai-control/ssh/ai_control_ed25519`» больше не является целевой моделью.

Собственная identity 301 создаётся общим механизмом:

```yaml
management:
  ssh_identity: true
```

во время `deploy-guest`:

```text
private
→ /etc/proxmox-guest/ssh/management_ed25519
→ остаётся только внутри 301

public
→ /etc/proxmox-guest/ssh/management_ed25519.pub
→ deployer забирает только открытую часть
→ регистрирует как /var/lib/proxmox-deployer/public-keys/301.pub
→ sync-management-keys распространяет её всем участвующим Debian-гостям с tag management-ssh
```

Имя файла связано только с VMID. Переименование `301` не меняет identity и не переименовывает `301.pub`.

Не использовать эту пару как GitHub credential.

## 6. Read-only доступ к проектному Git

Для чтения:

```text
git@github.com:zsergeyru/proxmox.git
```

301 не создаёт отдельный read-only Deploy Key.

После реализации `management.project_repo_read`:

```text
guest.yaml: management.project_repo_read=true
→ root-wrapper на PVE открывает фиксированный общий Git READ key
→ передаёт его текущему deploy через отдельный FD
→ deploy-guest по проверенному SSH root материализует локальную копию
→ /etc/proxmox-guest/credentials/github-proxmox-read
→ root:root 0600
→ проверить fingerprint и read-only доступ
```

Рабочая копия проекта:

```text
/opt/ai-control/repos/proxmox
```

Общий Git read-key и management SSH key не взаимозаменяемы, хотя запросы на оба находятся в одном manifest-разделе `management`.

Если AI потребуется GitHub write, это отдельный контур и отдельный credential.

## 7. Настройка Proximo

Установить и настроить Proximo как общий MCP к Proxmox.

Учётная запись:

```text
ai-agent@pve!infra
```

Проверить минимум:

```text
аутентификация API
чтение состояния разрешённых объектов
право клонирования шаблона 9000
право изменения объектов в /pool/managed
возможность устанавливать технический tag management-ssh на создаваемый разрешённый объект
отсутствие ожидаемо запрещённых прав уровня PVE-хоста
```

Точная матрица ACL определяется [`25-pve-access-control.md`](25-pve-access-control.md).

## 8. Установка AI-агента

Программное обеспечение конкретного агента размещается в:

```text
/opt/ai-control/agents/<agent>/
```

Hermes или другой агент должен использовать общую платформу:

```text
общий Proximo
общая рабочая копия проекта
management private key 301 в стандартном guest path
локально синхронизированный management-authorized-keys
общий read-only project Git credential гостя
```

## 9. Проверка создания новой Debian VM/LXC агентом

После ввода Proximo выполнить контролируемую проверку.

Перед созданием AI читает:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

Последовательность для Debian-гостя management-контура:

```text
AI-агент
→ прочитать текущий management-authorized-keys
→ Proximo
→ клонировать или создать тестовую Debian VM/LXC
→ сразу поместить в pool=managed
→ настроить CPU/RAM/диск/сеть
→ передать ВЕСЬ management-authorized-keys
→ установить PVE tag management-ssh
→ запустить
→ проверить
```

Проверить:

1. тестовый объект действительно создан в `managed`;
2. на тестовом объекте присутствует tag `management-ssh`;
3. AI может выполнять разрешённые операции уровня VM/LXC;
4. шаблон `9000` не стал гостем `managed` и не получил `management-ssh`;
5. AI не получил широкого доступа уровня PVE-хоста;
6. прямой SSH AI работает private key 301;
7. прямой SSH deployer также работает, потому что новая машина получила `deployer.pub`;
8. ручной `sync-management-keys` на PVE обнаруживает этот guest по tag, при первом подключении сохраняет SSH host key ожидаемого адреса и затем может обновлять его со строгой проверкой этого ключа.

AI не вызывает `deploy-guest` на PVE и не пишет в файловую систему PVE.

## 10. Проверка независимости ACL PVE и management

Подтвердить независимость:

```text
членство в managed
→ права на жизненный цикл и конфигурацию в PVE

PVE tag management-ssh
→ участие в области обнаружения sync-management-keys

management public keys в authorized_keys
→ прямой SSH внутрь ОС

management.project_repo_read
→ исходящий read-only Git access
```

Изменение одного уровня не должно неявно расширять другой.

## 11. Проверка синхронизации после появления 311

После того как `311-dev-services` будет развёрнут с:

```yaml
management:
  ssh_identity: true
```

```text
311 создаёт собственную pair внутри себя
→ deploy-guest забирает только 311 .pub
→ регистрирует её как /var/lib/proxmox-deployer/public-keys/311.pub
→ sync-management-keys
→ 301 получает обновлённый management-authorized-keys
```

301 не подключается к 311 для получения `.pub` и не подключается к PVE для записи registry.

Проверить, что локальный каталог 301 теперь содержит `311.pub`, а уже существующие Debian-гости с tag `management-ssh` получили его в управляемый блок `authorized_keys`.

## 12. Передача повторяемой настройки Ansible

После готовности 311:

```text
301 AI / оператор
→ конфигурация в Git
→ Ansible на 311
→ SSH root private management key 311
→ целевая гостевая система
```

Прямой SSH AI и SSH Ansible используют разные private keys, но их public части распространяются одним `sync-management-keys`.

## 13. Резервное копирование и восстановление

До признания `301` готовым:

- включить резервное копирование VM согласно [`22-storage-and-backup.md`](22-storage-and-backup.md);
- считать backup 301 чувствительным, потому что он содержит management private key и другие credentials;
- проверить восстановление `/etc/proxmox-guest/ssh/management_ed25519`;
- для общего Git read-key считать PVE мастер-копией, а копию 301 — воспроизводимой через deploy;
- локальный management public-key catalog считать воспроизводимым через `sync-management-keys`;
- выполнить практическую проверку восстановления согласно [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md).

Удаление 301 в будущем требует также удалить `/var/lib/proxmox-deployer/public-keys/301.pub` и выполнить `sync-management-keys`. Если VMID 301 затем используется для нового объекта, новая машина должна получить новую management identity; старый ключ не переиспользуется.

## 14. Приёмочные проверки

`301-ai-control` готов к замене устаревающего `320`, когда подтверждено:

```text
301 создаётся со стороны PVE без зависимости от работающего 301
QGA и Cloud-Init работают
PVE tag management-ssh установлен
SSH host key 301 сохранён для ожидаемого адреса и строго проверяется
SSH root со стороны deployer работает
management.ssh_identity 301 создана внутри гостя и private не покидал 301
/var/lib/proxmox-deployer/public-keys/301.pub зарегистрирован на PVE
sync-management-keys доставил полный public catalog в 301
management.project_repo_read materialized после реализации interface
рабочая копия проекта работает
Proximo входит как ai-agent@pve!infra
AI создаёт тестовую Debian VM/LXC сразу в managed
AI передаёт новой машине весь management-authorized-keys и ставит management-ssh
SSH AI и SSH deployer к новой машине работают разными private keys
AI не имеет SSH/root-доступа к PVE
появление management SSH identity 311 доставляется в 301 через PVE-side sync
Ansible/311 использует отдельный management private key
резервное копирование и восстановление проверены
```

До прохождения проверок `320-ai-control` не удаляется.

## 15. Ошибка и безопасное восстановление

Если настройка `301` остановилась:

```text
не удалять 320
не менять работающие credentials без необходимости
сохранить журналы и состояние
определить проблемный слой:
  guest / SSH / management.ssh_identity / key sync / management.project_repo_read / Proximo / agent
исправить предварительное условие
повторить только безопасный идемпотентный этап
```

Не генерировать новый management private key молча при неоднозначном состоянии существующей пары. Не удалять сохранённый SSH host key только ради того, чтобы принять неожиданно изменившийся ключ сервера.

Главное правило: **301 получает собственную management identity через `management.ssh_identity`, её открытая часть регистрируется как `/var/lib/proxmox-deployer/public-keys/301.pub`, полный набор открытых инфраструктурных ключей идёт только в направлении PVE → guest через `sync-management-keys`, а первое подключение к новой VM один раз сохраняет SSH host key ожидаемого адреса. SSH-доступ AI к PVE для этой схемы не нужен.**