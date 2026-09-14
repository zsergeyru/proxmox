# Docker внутри LXC — принятая политика

## Статус решения

**Принят вариант B:** сервисные application-контейнеры Docker разрешено запускать внутри unprivileged Proxmox LXC ради меньшего расхода RAM/CPU и более простой плотной консолидации сервисов.

Это осознанное отклонение от более изолированного варианта `Docker → QEMU VM`. Proxmox рассматривает LXC как system containers и для application containers рекомендует QEMU VM; проект принимает меньшую изоляцию только для доверенных домашних/инфраструктурных workload.

## Где применяется

Текущие Docker-LXC:

```text
211 automation-services
311 dev-services
321 app-services
331 ai-services
401 monitoring
```

В `guest.yaml` это фиксируется явно:

```yaml
lxc:
  container_runtime: docker
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
    download_if_missing: true
  unprivileged: true
  features:
    nesting: true
    keyctl: true
```

`container_runtime: docker` является машинно-читаемым признаком принятого исключения. CI запрещает Docker-LXC без `unprivileged: true`, `nesting: true` и `keyctl: true`.

## Почему выбран LXC

Для этих сервисов важнее:

- низкий runtime overhead;
- быстрый запуск/остановка;
- компактные backup;
- отсутствие необходимости держать отдельное полноценное ядро для каждого Docker host;
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

Для `lxc.container_runtime: docker` PVE-side deployer должен до APPLY/VERIFY проверить:

```text
unprivileged == true
nesting == true
keyctl == true
```

После настройки ОС должны успешно проходить как минимум:

```bash
docker version
docker info
docker compose version
```

Проверка реального application stack относится к Ansible/service health check соответствующего гостя.

## Backup / restore gate

Docker-LXC нельзя считать полностью проверенным только потому, что Docker запустился.

До перевода нового класса Docker-LXC в `active` требуется хотя бы один практический restore test:

```text
создать/настроить LXC
→ запустить Docker + тестовый/реальный Compose stack
→ сделать Proxmox backup
→ восстановить во временный CTID
→ запустить восстановленный LXC
→ docker info
→ docker compose config / service health
→ убедиться в сохранности persistent data
→ удалить временный restore после проверки
```

Restore test повторяется после существенного изменения:

- major-version Proxmox/LXC;
- базового Debian LXC template;
- security/features (`nesting`, `keyctl`, id mapping);
- схемы хранения Docker persistent data.

Обычный backup должен захватывать rootfs и те persistent данные, которые по проектной политике находятся внутри backup-able LXC storage. Внешние mount points/отдельные datasets должны иметь собственную backup policy.

## Обновления Docker

Версия Docker не должна бесконтрольно следовать `latest` в bootstrap/deploy scripts. Версии runtime/package фиксируются в принятом version lock и меняются отдельным commit с последующей проверкой Docker-LXC.

## Когда пересмотреть решение

Переход конкретного сервиса с LXC на VM рассматривается, если:

- появляются реальные проблемы совместимости Docker с LXC/cgroup/kernel;
- требуется сильная изоляция от PVE host;
- сервис исполняет недоверенный код;
- необходим passthrough, который существенно расширяет attack surface;
- backup/restore Docker-LXC оказывается ненадёжным;
- эксплуатационная сложность LXC становится выше экономии ресурсов.

## Главный принцип

> Docker внутри LXC в этом проекте разрешён не как универсальный default, а как контролируемый вариант B для доверенных сервисов: unprivileged LXC + `nesting` + `keyctl` + проверенный backup/restore.
