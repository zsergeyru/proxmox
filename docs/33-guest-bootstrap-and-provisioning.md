# Первичная и повторяемая настройка гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для Guest Bootstrap v1, границы `deploy-guest` ↔ Ansible и требований к готовности административного доступа.

Общая политика SSH — [`23-security.md`](23-security.md), management SSH keys — [`28-management-ssh-keys.md`](28-management-ssh-keys.md), manifest/schema — [`30-guest-manifest.md`](30-guest-manifest.md), общий PLAN/APPLY — [`31-deploy-guest.md`](31-deploy-guest.md). Текущая степень реализации отдельно фиксируется в [`29-implementation-status.md`](29-implementation-status.md).

## 1. Принятая модель

Guest Bootstrap входит в контракт первой версии `deploy-guest`:

```text
PVE lifecycle
→ current management-authorized-keys при создании
→ management-ssh tag
→ verified root SSH deployer
→ management.ssh_identity desired state
→ management.project_repo_read desired state
→ явно запрошенный Guest Bootstrap v1
→ final verify
→ Ansible
→ повторяемая настройка ОС и приложений
```

Ключевое правило:

> Административный SSH — часть готовности гостевой системы. Management identity, Project Git READ и Guest Bootstrap выполняются только после проверенного `root SSH` со стороны deployer.

Schema v7 уже содержит действующие машинные интерфейсы:

```text
management.ssh_identity
management.project_repo_read
bootstrap.capabilities
```

`provisioning` как отдельный высокоуровневый интерфейс пока не вводится.

## 2. Граница ответственности

`deploy-guest` отвечает за минимальную инфраструктурную готовность новой Debian VM/LXC:

```text
создать/найти объект
→ PVE state
→ initial management public keys
→ root SSH
→ optional management identity
→ optional Project Git READ
→ optional Bootstrap capabilities
→ acceptance
```

После этого долговременная конфигурация ОС и приложений выполняется Ansible.

`deploy-guest` не превращается во второй Ansible и не принимает из manifest произвольные package lists, shell commands, installer URLs или secret values.

## 3. Административный SSH

Для управляемых Debian VM/LXC:

```text
user: root
port: 22
root password: locked
password auth: disabled
public-key auth: enabled
```

Deployer использует постоянную пару:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Её public часть регистрируется как:

```text
/var/lib/proxmox-deployer/public-keys/deployer.pub
```

и входит в `management-authorized-keys`.

SSH host keys управляемых гостей хранятся отдельно:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

## 4. Начальный доступ к новой VM

Template 9000 не содержит постоянного project `authorized_keys`, management private keys или общего Git READ key.

Новый Full Clone получает до первого запуска:

```text
hostname / CPU / RAM / disk / network
→ ciuser=root
→ sshkeys=<management-authorized-keys>
→ PVE tag management-ssh
→ Cloud-Init update
→ start if requested
→ readiness
→ first SSH host-key trust for expected address
→ verified root SSH deployer
```

Никакие private management keys в обычную VM не передаются.

## 5. Начальный доступ к LXC

Для Debian LXC:

```text
resolve allowed Debian template
→ create LXC
→ ssh-public-keys=<management-authorized-keys>
→ PVE tag management-ssh
→ start if requested
→ readiness
→ first SSH host-key trust
→ verified root SSH deployer
```

Управляющий guest, создающий Debian VM/LXC напрямую через PVE API, использует локальную копию:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и также ставит новой машине `management-ssh`.

## 6. Management identity и Project Git READ

Эти механизмы относятся к ядру deploy/access, а не к Bootstrap capabilities.

### Собственная management SSH identity

```yaml
management:
  ssh_identity: true
```

означает создание/проверку пары:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Private остаётся только внутри guest, наружу регистрируется только `<VMID>.pub`. Полный lifecycle — [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

### Project Git READ

```yaml
management:
  project_repo_read: true
```

означает фиксированный read-only доступ только к `zsergeyru/proxmox`. Это отдельный credential, не management SSH key и не capability `git`.

При `false`/отсутствии поля ранее управляемые Project Git READ artifacts удаляются по контракту [`31-deploy-guest.md`](31-deploy-guest.md), но рабочие копии repo и чужие credentials не затрагиваются.

## 7. Guest Bootstrap v1

Bootstrap задаётся только в конкретном `guest.yaml`:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Он не наследуется из defaults/profile.

Разрешённый набор v1:

```text
base
git
docker
ansible_controller
```

Произвольные capability names запрещены schema.

Если `bootstrap` отсутствует, после management handlers выполняется final verify без установки Bootstrap-компонентов.

## 8. Зависимости и порядок

Зависимости должны быть указаны явно:

```text
base

git
└─ base

docker
└─ base

ansible_controller
├─ base
├─ git
└─ docker
```

Resolver не включает зависимости автоматически.

Канонический порядок:

```text
base → git → docker → ansible_controller
```

Bootstrap требует:

```yaml
boot:
  start_after_deploy: true
```

поскольку применяется через SSH после запуска.

## 9. PLAN

Без `--apply` Bootstrap не изменяет guest OS.

Для доступного существующего гостя PLAN может выполнять только read-only checks:

```text
Bootstrap
  base                APPLY | NO CHANGE | BLOCKED
  git                 APPLY | NO CHANGE | BLOCKED
  docker              APPLY | NO CHANGE | BLOCKED
  ansible_controller  APPLY | NO CHANGE | BLOCKED
```

Для нового ещё не запущенного объекта:

```text
APPLY AFTER START
```

PLAN не имеет права:

```text
apt install/update
generate management private key
register .pub
write Project Git credential
run sync-management-keys
change services
```

## 10. Общая модель APPLY capability

Каждый handler:

```text
read-only check
→ если соответствует: NO CHANGE
→ иначе: apply
→ final verify
```

Все remote operations имеют конечные timeout.

Если `apt/dpkg` обнаружен в повреждённом/незавершённом состоянии => STOP; скрытый автоматический repair запрещён.

`apt-get update` допустим только как подготовка к реально требуемой установке capability.

В Bootstrap v1 запрещены:

```text
apt upgrade
apt full-upgrade
apt dist-upgrade
apt autoremove
automatic purge/remove
automatic reboot/shutdown
curl ... | sh
wget ... | sh
TLS verification disable
```

Capability может перезапустить только принадлежащий ей сервис и только если это необходимо для достижения desired state.

## 11. `base`

`base` гарантирует минимальные системные предпосылки:

```text
python3
python3-apt
ca-certificates
rsync
```

Read-only check подтверждает работоспособность Python, module `apt`, CA bundle и `rsync`.

`base` не владеет OpenSSH, Git, Docker, Ansible, locale/timezone или прикладными пакетами.

Интерфейс вида:

```yaml
bootstrap:
  packages:
    - nginx
```

запрещён.

## 12. `git`

`git` обеспечивает наличие рабочего Git client.

Capability **не выдаёт GitHub credential**, не выбирает repository и не клонирует произвольный repo из manifest.

Разделение:

```text
bootstrap.capabilities.git=true
→ Git client должен работать

management.project_repo_read=true
→ фиксированный Project Git READ credential должен работать
```

Они независимы.

## 13. `docker`

`docker` обеспечивает проектно одобренный Docker Engine + Compose plugin.

Для Docker в LXC должны выполняться требования [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md), включая:

```text
unprivileged=true
nesting=true
keyctl=true
```

Final verify минимум:

```bash
docker version
docker info
docker compose version
```

Уже установленная приемлемая версия не обновляется на каждом deploy только потому, что появилась новая.

## 14. `ansible_controller`

Capability предназначена для `311-dev-services` и обеспечивает управляющую Ansible/Execution Environment поверх Docker.

Она **не создаёт отдельный SSH keypair**. Identity 311 задаётся:

```yaml
management:
  ssh_identity: true
```

Capability не устанавливает Semaphore, Gitea/Gogs, Jenkins и другие прикладные DevOps-сервисы; они относятся к штатному Ansible provisioning.

## 15. Ошибки и `deploy-incomplete`

Requested management state и Bootstrap входят в полную приёмку deploy.

Если required handler не завершился:

```text
→ deploy FAILED
→ следующие зависимые шаги не выполняются
→ destructive rollback не выполняется
→ объект сохраняется
→ deploy-incomplete остаётся
→ audit фиксирует шаг/ошибку без secrets
```

Следующий запуск заново читает actual state и выполняет только недостающее.

`deploy-incomplete` снимается только после:

```text
PVE verified
+ root SSH verified
+ requested management.ssh_identity verified/synced
+ Project Git READ desired state verified
+ requested Bootstrap capabilities verified
+ final PVE verify
```

Read-only PLAN и полный `NO CHANGE` tag не создают.

## 16. Передача управления Ansible

После Bootstrap повторяемая конфигурация ОС и приложений выполняется Ansible с `311-dev-services`.

Знания о конкретных сервисах находятся в roles/playbooks и guest-specific проектных файлах, а не в `deploy-guest.py`.

Будущий интерфейс `provisioning` может быть введён отдельно; сейчас он не реализуется скрыто через Bootstrap.

## 17. `311-dev-services`

Manifest 311 в schema v7 уже содержит:

```yaml
management:
  ssh_identity: true
  project_repo_read: true

bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Целевой deploy flow:

```text
create LXC 311 with management-authorized-keys
→ management-ssh tag
→ verified root SSH deployer
→ own management keypair 311
→ register 311.pub
→ sync-management-keys
→ Project Git READ desired state
→ base
→ git
→ docker
→ ansible_controller
→ final acceptance
→ SUCCESS
```

После этого Semaphore, Git service, CI и другие приложения устанавливаются Ansible.

## 18. Обычные Debian-гости

Обычный deployable Debian guest без `ssh_identity`, `project_repo_read` и `bootstrap` всё равно получает:

```text
management.ssh из defaults
management-authorized-keys
PVE tag management-ssh
root SSH deployer
```

После этого он может конфигурироваться с 311.

Гость может запросить только нужный поднабор Bootstrap, если зависимости явно соблюдены.

## 19. Структура реализации

Целевое логическое разделение:

```text
scripts/pve/deploy-guest.py
→ PVE lifecycle + access + management handlers

scripts/pve/sync-management-keys.py
→ массовая public-key sync

bootstrap handlers
→ base / git / docker / ansible_controller

scripts/guest_config.py
→ единый resolver validator + deployer
```

Точные filenames runtime modules могут быть уточнены при реализации, но граница ответственности сохраняется.

## 20. Ограничения безопасности

- secrets/private keys не хранятся в `guest.yaml`;
- manifest не выбирает secret path/name;
- `management.ssh_identity` — только boolean;
- `management.project_repo_read` — только boolean;
- individual management flags не наследуются из defaults/profile;
- guest management private key остаётся у владельца;
- Project Git master key на PVE остаётся root-only;
- read key передаётся runtime только через отдельный FD;
- Project Git READ не превращается в write credential;
- Bootstrap не принимает произвольный shell/package interface;
- handlers не печатают secret contents или полный environment.

## 21. Состояние реализации

Этот документ задаёт **действующий контракт**, а не текущую готовность всех runtime handlers.

Schema v7 и resolver уже поддерживают management fields и `bootstrap.capabilities`; manifests `301`/`311` могут содержать принятый desired state. Какие runtime-компоненты уже написаны, фиксируется только в:

[`29-implementation-status.md`](29-implementation-status.md)

Расширенные CI-проверки management/runtime будут добавлены вместе с реальными `deploy-guest`/`sync-management-keys`, а не заранее для несуществующих handlers.

## 22. Главный принцип

> Guest Bootstrap доводит Debian VM/LXC от verified administrative SSH до минимально требуемой инфраструктурной готовности. Management SSH identity и Project Git READ остаются отдельными access-механизмами ядра deploy, а всё прикладное и долговременное управление передаётся Ansible.