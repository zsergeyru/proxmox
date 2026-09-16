# Файловая структура Public Bootstrap / PVE Configuration

Парный документ: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Public Bootstrap
→ временный runtime только для первоначального доступа к private repo
→ /var/lib/proxmox-bootstrap после успешного first run удаляется

PVE Configuration
→ создаёт и обслуживает постоянную инфраструктурную структуру
→ state хранится в /var/lib/proxmox-deployer/state

оба компонента
→ используют /run/lock/proxmox-orchestration.lock

trust boundary
→ root владеет canonical source/Git credential
→ pvedeploy владеет только своим mutable deployment runtime
```

## 1. Public Bootstrap

Публичный репозиторий:

```text
zsergeyru/proxmox-bootstrap
└── bootstrap-pve.sh
```

До первого успешного handoff используется временная область:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Каталог создаётся как `root:root 0700`.

`private-repo/` является disposable checkout. При resume он может быть приведён к свежему `FETCH_HEAD` через `reset --hard` и `git clean -ffdx`, включая ignored cache/build artifacts. Это правило относится только к temporary runtime.

После успешной PVE Configuration Public Bootstrap удаляет `/var/lib/proxmox-bootstrap` и записывает:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

## 2. PVE Configuration source

Канонический private entrypoint:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
```

Структура:

```text
scripts/pve/setup/
├── configure-pve.sh
├── render-template-cloud-init.py
├── tests/
│   ├── test-template-contract.sh
│   ├── test-template-guest-exec.sh
│   ├── test-template-smoke.sh
│   ├── test-token-rollback.sh
│   └── test-source-trust.sh
└── lib/
    ├── 00-common.sh
    ├── 10-preflight.sh
    ├── 20-system.sh
    ├── 30-storage.sh
    ├── 40-runtime.sh
    ├── 50-access.sh
    ├── 60-template-contract.sh
    ├── 61-template-source.sh
    ├── 62-template-build.sh
    ├── 63-template-smoke.sh
    └── 70-tooling.sh
```

Канонический постоянный checkout:

```text
/var/lib/proxmox-deployer/repo/
```

Это **root-trusted runtime-copy** private source of truth:

```text
owner: root:root для repo tree
write: запрещён group/other
parent /var/lib/proxmox-deployer: root:pvedeploy 0750
```

Родитель намеренно не writable для `pvedeploy`: иначе ограниченный пользователь мог бы удалить/переименовать даже root-owned `repo/` и заменить его целиком.

Перед автоматическим Git refresh проверяется полный clean state; local tracked/staged/untracked/ignored drift не стирается молча. В отличие от temporary checkout, `git clean -ffdx` здесь не применяется.

`pvedeploy` может читать source через group traverse родителя и обычные read bits файлов, но не может писать в repo или `.git`. Отдельного `scripts/pve/create-template.sh` нет. `deploy-guest.py` появится после реализации deployer.

## 3. Общая orchestration lock

```text
/run/lock/proxmox-orchestration.lock
```

Public Bootstrap открывает lock на fd 9 и передаёт этот fd PVE Configuration. Прямой `configure-pve.sh` берёт тот же lock самостоятельно.

Lock защищает одновременно canonical private checkout, permanent runtime mutation, host configuration, Template 9000 pipeline, Full Clone smoke VMID 9099 и state/final marker transitions.

Lock дополняет, но не заменяет filesystem trust boundary: `pvedeploy` физически не имеет права записи в canonical source.

## 4. Постоянная конфигурация deployer

```text
/etc/proxmox-deployer/
├── config.yaml
├── ssh/
│   ├── github_proxmox_repo_ed25519
│   ├── github_proxmox_repo_ed25519.pub
│   ├── pve_guest_ed25519
│   ├── pve_guest_ed25519.pub
│   ├── config
│   └── known_hosts
└── secrets/
    ├── host-deploy.token
    └── ai-agent-infra.token
```

Назначение:

```text
config.yaml
→ постоянные параметры host-side deployer

ssh/github_proxmox_repo_ed25519
→ root-only canonical read-only Deploy Key для zsergeyru/proxmox

ssh/pve_guest_ed25519
→ pvedeploy-owned SSH identity для управляемых VM/LXC
→ этим же key PVE Configuration проверяет реальный root SSH smoke clone

ssh/config + known_hosts
→ root-owned SSH transport private Git checkout

secrets/host-deploy.token
→ credential deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ credential ai-agent@pve!infra
```

Canonical SSH config включает strict host-key checking, `BatchMode yes`, bounded `ConnectTimeout` и server-alive policy.

## 5. Права и Linux runtime user

Ключевое разделение:

```text
/etc/proxmox-deployer/                     root:root
/etc/proxmox-deployer/ssh/                root:pvedeploy 0750
github_proxmox_repo_ed25519                root:root 0600
github_proxmox_repo_ed25519.pub            root:root 0644
ssh/config                                 root:root 0600
ssh/known_hosts                            root:root 0644
pve_guest_ed25519                          pvedeploy:pvedeploy 0600
pve_guest_ed25519.pub                      pvedeploy:pvedeploy 0644
/etc/proxmox-deployer/secrets/             root:pvedeploy 0710
host-deploy.token                          root:pvedeploy 0640
ai-agent-infra.token                       root:root 0600
/var/lib/proxmox-deployer                  root:pvedeploy 0750
/var/lib/proxmox-deployer/repo             root-owned, group/other non-writable
/var/lib/proxmox-deployer/state            root:pvedeploy 0750
/var/lib/proxmox-deployer/cache            pvedeploy:pvedeploy 0750
/var/log/proxmox-deployer                  root:pvedeploy 0750
/var/log/proxmox-deployer/audit            pvedeploy:pvedeploy 0750
```

`pvedeploy` contract:

```text
system UID < 1000
primary group = pvedeploy
home = /var/lib/pvedeploy
shell = /bin/bash
```

Таким образом будущий `deploy-guest`, запущенный с ограниченными правами, сможет читать manifests/source и использовать свои runtime credentials, но не сможет изменить код или Git metadata, который затем исполняет `root`.

Secrets создаются с безопасным `umask`, не попадают в Git и не выводятся в обычные logs. Новый API token secret записывается атомарно; обычный failure/interruption откатывает только newly-created pending token.

## 6. Mutable runtime

```text
/var/lib/proxmox-deployer/
├── repo/    root-trusted, immutable для pvedeploy
├── state/   orchestration + smoke state
└── cache/   mutable pvedeploy runtime
```

Private SSH keys и token secrets здесь не хранятся.

Template image cache:

```text
/var/lib/vz/template/cache/debian13/
```

Temporary Cloud-Init builder snippet создаётся в `local:snippets` только на время сборки 9000. При существующей unfinished builder VM автоматический cleanup не выполняется.

Smoke-specific ephemeral host file:

```text
/run/pve-template-smoke-known-hosts.XXXXXX
```

Он используется только для проверки SSH host key временной VM `9099` до/после reboot и удаляется после success либо при обычном EXIT/INT/TERM. Failed VM `9099` при этом не удаляется.

## 7. State

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
├── template-smoke.json
└── bootstrap-complete
```

Назначение:

```text
state.json
→ текущее состояние PVE Configuration

version
→ PVE_CONFIGURATION_VERSION

last-run.json
→ timestamp/result/source revision без secrets

last-revision
→ revision canonical private checkout

template-smoke.json
→ pending/passed состояние реального Full Clone smoke-test template 9000
→ в passed state хранит revision, machine-id, kernel, rootfs bytes и SSH host-key fingerprint тестового clone

bootstrap-complete
→ первоначальный Public Bootstrap успешно завершён
```

`state.json`, `last-run.json`, `version` и `template-smoke.json` записываются через temporary file + atomic `mv`.

`template-smoke.json=status=pending` является durable resume marker: после interruption следующий обычный PVE Configuration обязан вернуться к smoke-test. `passed` записывается только после полного smoke success и удаления VMID `9099`.

State не является единственным source of truth: каждый запуск перепроверяет PVE, credentials, Git state, source ownership и фактическое состояние VMID 9000/9099.

## 8. Logs

```text
/var/log/proxmox-deployer/
├── configure-pve.log
└── audit/
```

Создание template 9000 и Full Clone smoke-test 9099 пишут в тот же `configure-pve.log`; отдельных orchestrator/log pipeline нет.

Правила: persistent log без ANSI, без token secrets/private keys, с operations/warnings/revision и без полного environment dump.

## 9. Configuration snapshots

```text
/var/backups/proxmox-configuration/YYYYMMDD-HHMMSS/
```

Это snapshot host configuration/diagnostics перед значимыми изменениями, а не VM backup. Не архивировать бездумно весь `/etc/pve`, так как это `pmxcfs`.

## 10. Backup infrastructure secrets

Защищённая локальная область:

```text
/var/backups/proxmox-secrets/
```

Внешняя backup policy должна включать как минимум token secrets, canonical GitHub Deploy Key, PVE guest SSH keypair, SSH config и known_hosts. Локальная копия на том же SSD не считается полноценным disaster-recovery backup.

## 11. Стабильные команды

```text
/usr/local/sbin/
├── pve-configuration-status
└── deploy-guest
```

`pve-configuration-status` читает `state.json`.

`deploy-guest` после реализации будет небольшой wrapper-командой к source из canonical private checkout. Его runtime не получает write-доступ к canonical source.

Smoke-test не устанавливает отдельную постоянную CLI-команду: штатный явный запуск идёт через `bootstrap-pve.sh --smoke-test-template` либо напрямую через `configure-pve.sh --smoke-test-template`.

## 12. Итоговое дерево

```text
/
├── etc/proxmox-deployer/
│   ├── config.yaml
│   ├── ssh/
│   └── secrets/
├── run/lock/
│   └── proxmox-orchestration.lock
├── var/lib/proxmox-deployer/       root:pvedeploy 0750
│   ├── repo/                       root-owned
│   ├── state/                      root:pvedeploy
│   │   └── template-smoke.json
│   └── cache/                      pvedeploy:pvedeploy
├── var/log/proxmox-deployer/       root:pvedeploy
│   └── audit/                      pvedeploy:pvedeploy
├── var/backups/proxmox-configuration/
├── var/backups/proxmox-secrets/
└── usr/local/sbin/
    ├── pve-configuration-status
    └── deploy-guest
```

Главный принцип:

> Public Bootstrap использует отдельную temporary область только для первоначального доступа. Canonical source и Git credential принадлежат root; `pvedeploy` получает только нужный mutable runtime. Smoke state durable и fail-closed: failed clone сохраняется для диагностики, а `passed` возникает только после реального Full Clone lifecycle и успешного удаления временной VM.
