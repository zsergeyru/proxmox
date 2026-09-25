# bootstrap-runner

Этот каталог содержит закрытую часть временного LXC `990 bootstrap-runner`.

## Граница ответственности

Код здесь не создаёт сам LXC 990. Это минимальная обязанность public bootstrap.

После получения закрытого проекта 990 использует этот каталог для подготовки одноразовой среды выполнения и запуска общего механизма развёртывания VMID 910.

Основная логика OpenTofu и Ansible сюда не копируется.

## Файлы

~~~text
pve-access.sh
→ выполняется от root на PVE
→ выдаёт временный token 990
→ передаёт PVE CA
→ передаёт стабильный bootstrap SSH key
→ передаёт существующий 910.tfstate
→ после применения сохраняет state обратно
→ удаляет временный token

run.sh deploy
→ выполняется внутри 990
→ подготавливает Docker
→ собирает общий infra-runtime
→ запускает deploy-910.py

deploy-910.py
→ вызывает run_deploy_guest(REPO_ROOT, 910, exclusive=True)
→ использует общий OpenTofu и общий Ansible
→ отдельный state содержит только 910

run.sh configure
→ передаёт выбранную ревизию проекта в 910
→ передаёт read-only GitHub Deploy Key
→ запускает существующий infra-manager setup
→ выполняет infra-manager-status --full
~~~

## Состояние

Постоянный bootstrap-state и ключ находятся на PVE:

~~~text
/var/lib/proxmox-bootstrap/
├── state/910.tfstate
└── ssh/
    ├── 910-bootstrap-ed25519
    └── 910-bootstrap-ed25519.pub
~~~

Рабочие копии внутри 990 являются временными.

При ошибке 990 не удаляется автоматически, потому что его рабочий state может быть новее сохранённой на PVE копии.

## Порядок вызова

~~~text
public bootstrap создаёт 990
→ pve-access.sh apply на PVE
→ run.sh deploy внутри 990
→ scripts/infra-manager/pve-bootstrap-access.sh на PVE для постоянного доступа 910
→ run.sh configure внутри 990
→ pve-access.sh save-state на PVE
→ pve-access.sh cleanup на PVE
→ удалить 990
~~~

Подробный контракт: [docs/200-pve/215-bootstrap-runner.md](../../docs/200-pve/215-bootstrap-runner.md).
