# Инициализация нового Proxmox VE host

**Type:** Runbook  
**Status:** Active  
**Source of truth:** Yes — для порядка запуска Public Bootstrap / PVE Configuration и операторских действий при first run, rerun и smoke-test.

Этот документ отвечает на вопрос **«что запускать и в каком порядке»**. Он не дублирует точные filesystem permissions, versioning policy, PVE ACL или contract template `9000`.

Связанные канонические документы:

- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — host-side каталоги, state и credentials;
- [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) — versioning/reproducibility policy;
- [`25-pve-access-control.md`](25-pve-access-control.md) — PVE identities, roles, privileges и ACL;
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — contract template `9000`;
- [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md) — восстановление после потери host.

## 1. Когда использовать этот runbook

Использовать его для:

- первого запуска проекта на чистом Proxmox VE;
- обычного повторного применения host configuration;
- продолжения после исправимой ошибки предыдущего запуска;
- явного полного обновления системы;
- явного smoke-test уже существующего template `9000`.

Обычный rerun должен быть штатным сценарием. Не требуется вручную удалять runtime или credentials только потому, что предыдущий запуск завершился ошибкой.

## 2. Предварительные условия

Перед запуском:

1. установлен поддерживаемый Proxmox VE host;
2. есть `root` shell;
3. работает DNS и исходящий HTTPS/SSH к GitHub;
4. public bootstrap repository доступен;
5. при первом запуске можно зарегистрировать read-only Deploy Key для private repository;
6. на host нет другого одновременно работающего bootstrap/configuration процесса.

Public Bootstrap и PVE Configuration используют общую orchestration lock:

```text
/run/lock/proxmox-orchestration.lock
```

Параллельные runs запрещены.

## 3. Обычный запуск

Запускать от `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash
```

Эта же команда используется для:

- первого запуска;
- обычного rerun;
- resume после исправления причины предыдущей ошибки.

## 4. Что происходит на первом запуске

Высокоуровневая последовательность:

```text
проверка root / Proxmox
→ общая orchestration lock
→ минимальный Git/SSH runtime
→ проверка GitHub connectivity
→ временный read-only Deploy Key
→ временный root-owned checkout private repo
→ фиксация exact Git revision текущего run
→ запуск PVE Configuration
→ создание permanent root-trusted runtime
→ проверка host configuration
→ template 9000 build/verification при необходимости
→ Full Clone smoke-test после новой сборки
→ cleanup temporary bootstrap runtime
→ marker успешного bootstrap
```

Точные пути и ownership не повторяются здесь; они определены в [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md).

## 5. Повторный запуск

При rerun Public Bootstrap должен использовать permanent root-trusted checkout.

Нормальный flow:

```text
взять orchestration lock
→ проверить trust boundary и clean Git state
→ получить актуальный main
→ зафиксировать exact revision
→ запустить PVE Configuration на этой revision
→ выполнить только необходимые изменения
→ verify
```

Если permanent checkout содержит локальный drift, запуск должен остановиться. Локальные tracked/staged/untracked/ignored изменения не стираются молча.

## 6. Resume после ошибки

После исправления причины ошибки повторить обычную команду bootstrap.

Не выполнять без отдельной причины:

```text
rm -rf /etc/proxmox-deployer
rm -rf /var/lib/proxmox-deployer
```

Существующие API-token secrets, SSH private keys и другие постоянные credentials не должны автоматически ротироваться при обычном rerun.

Если предыдущая попытка оставила диагностический builder/template/smoke object, PVE Configuration должна остановиться или продолжить по соответствующей safety policy, а не уничтожать неоднозначный объект автоматически.

## 7. Полное системное обновление

Полное обновление Debian/Proxmox выполняется только по явному запросу:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --update-system
```

Обычный bootstrap не должен незаметно превращаться в full system upgrade.

## 8. Явный smoke-test template `9000`

Для уже существующего template:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --smoke-test-template
```

После новой сборки `9000` smoke-test выполняется автоматически и отдельный флаг не нужен.

Smoke-test использует проектный VMID `9099`. При успехе test guest удаляется; при ошибке он сохраняется для диагностики. Полный contract находится в [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

## 9. Прямой запуск PVE Configuration

Штатный пользовательский entrypoint — Public Bootstrap. Прямой запуск `configure-pve.sh` допустим для разработки/диагностики только из корректного root-trusted project source.

Entry point:

```text
scripts/pve/setup/configure-pve.sh
```

Прямой запуск использует ту же orchestration lock и те же safety checks.

## 10. Что проверить после успешного run

Минимальная операторская проверка:

```text
PVE Configuration завершилась SUCCESS
canonical checkout clean и доступен
state/last-run обновлены
storage доступен
project identities/ACL прошли effective-permission checks
template 9000 соответствует contract
если smoke был нужен — он завершился passed
нет оставленного unexpected builder/smoke guest
```

Дополнительно проверить фактическое состояние host в [`../host/pve/README.md`](../host/pve/README.md) и профильных `host/pve/*` документах.

## 11. Если запуск остановился

Не пытаться обходить STOP вручную destructive-командами до понимания причины.

Порядок действий:

```text
прочитать финальную ошибку и log
→ определить предметную область
→ исправить prerequisite / drift / conflict
→ сохранить диагностический объект, если на него указывает ошибка
→ повторить обычный bootstrap
```

Типовые владельцы contract:

| Проблема | Документ |
|---|---|
| Git/source trust, версии, state | [`24-reproducible-bootstrap.md`](24-reproducible-bootstrap.md) |
| Каталоги/ownership/credentials | [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) |
| API identities/roles/ACL | [`25-pve-access-control.md`](25-pve-access-control.md) |
| Template 9000 / smoke | [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) |
| Storage/backup policy | [`22-storage-and-backup.md`](22-storage-and-backup.md) |
| Disaster recovery | [`27-backup-and-disaster-recovery-runbook.md`](27-backup-and-disaster-recovery-runbook.md) |

## 12. Результат

Успешный run должен привести host к состоянию, в котором:

```text
private project source доверенно доступен на PVE
PVE Configuration может безопасно rerun
host-side runtime и credentials подготовлены
PVE identities/ACL готовы
Debian LXC source подготовлен
current template 9000 валиден
smoke state согласован
будущий deploy-guest имеет подготовленную host-side основу
```

Точные значения этих contracts принадлежат профильным Specification/Reference/Policy документам, а не этому runbook.