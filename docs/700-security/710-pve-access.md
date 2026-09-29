# Доступ 910 к Proxmox VE

## 1. Принятая модель доверия

`910 infra-manager` является доверенным управляющим контуром домашнего PVE.

Компрометация 910 рассматривается как компрометация административного контура PVE. Поэтому для 910 не поддерживается отдельная сложная матрица ограниченных ACL.

Это решение относится только к 910. Прикладные гости и AI Control не получают прямой административный доступ к PVE.

## 2. Два канала управления

910 использует два канала:

```text
910
├── HTTPS API → OpenTofu и обычные операции PVE
└── root SSH → только операции, которые PVE запрещает API token
```

Основным способом управления остаётся HTTPS API.

Root SSH не заменяет OpenTofu и не создаёт отдельный путь развёртывания.

## 3. PVE API token

Постоянная идентичность:

```text
root@pam!infra-manager
privsep=0
```

При `privsep=0` token наследует права пользователя `root@pam`.

Отдельные ACL для 910 не создаются.

Secret хранится внутри 910:

```text
/etc/infra-manager/secrets/pve-api.env
```

PVE CA устанавливается в доверенное хранилище 910. Проверка TLS не отключается.

## 4. Root SSH

Для 910 создаётся отдельный ключ, не связанный с GitHub Deploy Key и ключом Ansible для гостей.

Внутри 910:

```text
/mnt/pve-access/pve-host/root_ed25519
/mnt/pve-access/pve-host/known_hosts
```

Ключ передаётся в `infra-runtime` только для чтения.

На PVE открытая часть добавляется в:

```text
/root/.ssh/authorized_keys
```

Закрытая исходная копия хранится в постоянном каталоге bootstrap на PVE, чтобы 910 можно было восстановить.

## 5. Зачем нужен root SSH

Некоторые свойства LXC Proxmox разрешает менять только самому `root@pam`, но не API token.

Текущий обязательный случай — `keyctl` для профиля `container-host`.

Логический контракт остаётся единым:

```text
container-host
├── nesting=1
└── keyctl=1
```

Реализация разделена технически:

```text
nesting=1
→ OpenTofu
→ PVE API

keyctl=1
→ общий deploy-guest
→ infra_manager.pve_host
→ root SSH
→ pct set
```

Для 910 и обычных Docker-LXC используется один и тот же общий механизм.

## 6. Pool managed

Pool `managed` сохраняется как организационный объект.

Он не является границей прав 910.

В дальнейшем pool может использоваться для ограничения менее доверенных контуров, например AI Control.

Сам 910 в `managed` не входит.

## 7. Первоначальная передача доступа

Закрытый `bootstrap-host.py`, выполняемый на PVE от root:

1. создаёт или восстанавливает `root@pam!infra-manager`;
2. при переходе со старой схемы удаляет старый token с `privsep=1` и его ACL;
3. передаёт новый API secret в 910;
4. передаёт PVE CA;
5. подготавливает отдельный root SSH-ключ;
6. передаёт ключ и `known_hosts` в 910;
7. после общей настройки Ansible постоянные данные остаются в `/etc/infra-manager/`.

Отдельный `pve-bootstrap-access.sh` больше не используется.

## 8. Восстановление

Если API secret потерян, используется:

```text
bootstrap-pve.sh --recover
```

Режим восстановления перевыпускает API token и повторно передаёт требуемые данные.

Root SSH-ключ может быть восстановлен из постоянного bootstrap-каталога PVE.

## 9. Удаление

`--remove` удаляет 910 и отзывает его открытый root SSH-ключ из `/root/.ssh/authorized_keys`.

Закрытая исходная копия ключа может оставаться в каталоге bootstrap для повторного развёртывания.

`--purge` дополнительно удаляет постоянный bootstrap-каталог и его ключи.

## 10. Проверка

Внутри 910:

```bash
infra-manager-pve-access-check
infra-manager-status --full
```

Проверка подтверждает:

- доступ к PVE API;
- административные права token;
- доступность `managed`;
- root SSH от 910 к PVE;
- строгую проверку SSH host key.

## 11. Граница AI Control

AI Control не получает:

- API secret `root@pam!infra-manager`;
- root SSH-ключ PVE;
- прямой доступ к `pct`, `qm` или shell PVE.

AI должен выполнять разрешённые инфраструктурные действия через Semaphore и штатные задания 910.

## 12. Связанные документы

- [`700-overview.md`](700-overview.md) — общая модель безопасности.
- [`720-ssh-access.md`](720-ssh-access.md) — SSH-идентичности.
- [`../200-pve/210-host-bootstrap.md`](../200-pve/210-host-bootstrap.md) — первоначальное создание 910.
- [`../800-operations/810-deployment.md`](../800-operations/810-deployment.md) — штатное развёртывание.
