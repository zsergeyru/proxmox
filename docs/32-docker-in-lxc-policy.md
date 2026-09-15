# Docker внутри LXC — принятая политика

## Статус решения

**Принят вариант B:** сервисные application-контейнеры Docker разрешено запускать внутри unprivileged Proxmox LXC ради меньшего расхода RAM/CPU и более простой плотной консолидации сервисов.

Это осознанное отклонение от более изолированного варианта `Docker → QEMU VM`. Проект принимает меньшую изоляцию только для доверенных домашних/инфраструктурных workload.

## Где применяется

Текущие Docker-LXC:

```text
211 automation-services
311 dev-services
321 app-services
331 ai-services
401 monitoring
```

В effective profile это фиксируется так:

```yaml
lxc:
  container_runtime: docker
  source:
    ostemplate: local:vztmpl/debian-13-standard
  unprivileged: true
  features:
    nesting: true
    keyctl: true
```

`container_runtime: docker` является машинно-читаемым признаком принятого исключения. CI запрещает Docker-LXC без `unprivileged: true`, `nesting: true` и `keyctl: true`.

## LXC source

`lxc.source.ostemplate` — **selector семейства**, а не конкретное имя archive.

В Git хранится:

```text
local:vztmpl/debian-13-standard
```

Не хранится:

```text
debian-13-standard_13.6-1_amd64.tar.zst
```

PVE Stage 1 автономно выполняет `pveam update`, определяет актуальный Debian 13 standard archive и при необходимости скачивает его в `local:vztmpl`.

Будущий `deploy-guest`:

```text
selector из effective state
→ найти установленные archives этого семейства на разрешённом storage
→ выбрать наиболее новую доступную версию
→ если ничего не найдено — BLOCKED/STOP
```

`deploy-guest` не скачивает LXC appliance сам и не подменяет Debian 13 другим семейством.

Это позволяет не привязывать guest manifests к timestamp/package revision Proxmox appliance и одновременно не выдавать AI право `Datastore.AllocateTemplate`.

## Management access

Docker-LXC подчиняется общему guest contract:

```text
management user: root
password authentication: disabled
SSH: public keys only
```

При создании LXC initial public keys передаются через Proxmox `ssh-public-keys`. Получение root SSH не зависит от optional `base` capability.

## Почему выбран LXC

Для этих сервисов важнее:

- низкий runtime overhead;
- быстрый запуск/остановка;
- компактные backup;
- отсутствие отдельного полноценного ядра для каждого Docker host;
- простое распределение небольших сервисов по отдельным CTID.

Это не означает, что LXC считается эквивалентом VM по изоляции.

## Security boundary

Docker внутри LXC допускается только для **доверенных project workloads**.

По умолчанию запрещено:

- privileged LXC;
- `lxc.apparmor.profile=unconfined`;
- проброс Docker socket хоста PVE внутрь LXC;
- broad bind-mount системных каталогов PVE;
- произвольный device passthrough;
- `mknod`, FUSE и дополнительные mount features без отдельного решения;
- запуск недоверенного multi-tenant code/build workload;
- использование LXC как security sandbox для потенциально враждебного кода.

Если workload требует privileged container, нестандартных kernel capabilities, широкого device passthrough, недоверенных build jobs или более сильной security boundary, для него выбирается QEMU VM либо оформляется отдельное ADR.

## Обязательные проверки deployer

Для `lxc.container_runtime: docker` до APPLY/VERIFY проверяются:

```text
unprivileged == true
nesting == true
keyctl == true
resolved ostemplate принадлежит selector family
root SSH management key установлен
```

После настройки ОС должны успешно проходить как минимум:

```bash
docker version
docker info
docker compose version
```

Проверка application stack относится к Ansible/service health check соответствующего гостя.

## Backup / restore gate

Docker-LXC нельзя считать полностью проверенным только потому, что Docker запустился.

До перевода нового класса Docker-LXC в `active` требуется хотя бы один практический restore test:

```text
создать/настроить LXC
→ запустить Docker + тестовый/реальный Compose stack
→ сделать Proxmox backup
→ восстановить во временный CTID
→ запустить восстановленный LXC
→ проверить root SSH management access
→ docker info
→ docker compose config / service health
→ убедиться в сохранности persistent data
→ удалить временный restore после проверки
```

Restore test повторяется после существенного изменения:

- major-version Proxmox/LXC;
- семейства/существенного изменения базового Debian LXC appliance;
- security/features (`nesting`, `keyctl`, id mapping);
- схемы хранения Docker persistent data.

Обычный backup должен захватывать rootfs и persistent данные, которые по проектной политике находятся внутри backup-able LXC storage. Внешние mount points/отдельные datasets имеют собственную backup policy.

## Обновления Docker

Docker runtime не должен обновляться неконтролируемо самим `deploy-guest`. Управление версией/обновлением Docker относится к bootstrap/provisioning policy и изменяется отдельным project commit с последующей проверкой Docker-LXC.

Это правило не означает обязательный exact-version pin каждого Debian/Proxmox external package.

## Когда пересмотреть решение

Переход конкретного сервиса с LXC на VM рассматривается, если:

- появляются реальные проблемы совместимости Docker с LXC/cgroup/kernel;
- требуется сильная изоляция от PVE host;
- сервис исполняет недоверенный код;
- необходим passthrough, который существенно расширяет attack surface;
- backup/restore Docker-LXC оказывается ненадёжным;
- эксплуатационная сложность LXC становится выше экономии ресурсов.

## Главный принцип

> Docker внутри LXC — контролируемый вариант для доверенных сервисов: unprivileged LXC + `nesting` + `keyctl` + family selector Debian 13 + root key-only management + проверенный backup/restore.
