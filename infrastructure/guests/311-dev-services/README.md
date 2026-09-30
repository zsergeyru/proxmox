# 311 dev-services

`311 dev-services` — LXC для сервисов разработки проекта.

## Источники требований

| Источник | Что определяет |
|---|---|
| `guest.yaml` | объект LXC в Proxmox |
| `provision.yaml` | состав ОС, Docker и установленные средства |
| `../../security/access.yaml` | разрешённые внешние доступы |
| этот README | назначение и границы гостя |

Права доступа не задаются в `guest.yaml`.

## Объект Proxmox

Гость использует профиль:

```text
debian-lxc-docker
```

Текущие ресурсы:

```text
VMID: 311
CPU: 2
RAM: 4096 MB
swap: 1024 MB
disk: 32 GB
```

`pve_management` не переопределяется и наследует общее значение проекта.

## Состав ОС

`provision.yaml` требует:

- Debian 13 amd64;
- Git;
- Ansible Core;
- Docker из официального репозитория;
- включённую и запущенную службу Docker.

Конкретные прикладные сервисы разработки добавляются только вместе с их реальной реализацией.

## Доступы

Единственный машинный источник требований — `infrastructure/security/access.yaml`.

Для `guest:311` сейчас разрешены:

- выпуск собственной межмашинной SSH-идентичности;
- чтение репозитория `zsergeyru/proxmox`.

Само наличие SSH identity не даёт права входа в другие гости. Для такого входа требуется отдельное правило `ssh/guest/connect-root` в `access.yaml`.

Read-only Git credential материализуется общим Ansible-слоем в:

```text
/etc/proxmox-guest/git/github-proxmox-read
/etc/proxmox-guest/git/github-known-hosts
/etc/ssh/ssh_config.d/45-project-git-read.conf
```

Закрытый Git key не используется как SSH-идентичность администратора и не добавляется в `authorized_keys`.

Старые поля `management` и каталог `/etc/proxmox-guest/credentials/` не являются частью текущего контракта.

## Границы

311 не является управляющим контуром PVE. OpenTofu, Packer, централизованный Ansible и Semaphore инфраструктуры находятся в 910 `infra-manager`.

Установленный в 311 Ansible Core относится к среде разработки и сам по себе не создаёт никаких административных полномочий.

## Воспроизводимость

Конфигурация ОС и доступов должна повторно применяться через общий `Deploy Guest`.

Секреты не хранятся в Git. Git read-доступ может быть повторно выдан из OpenBao согласно `access.yaml`.

## Связанные файлы

- [`guest.yaml`](guest.yaml) — параметры LXC.
- [`provision.yaml`](provision.yaml) — состав ОС.
- [`../../security/access.yaml`](../../security/access.yaml) — машинные правила доступа.
- [`../../../docs/700-security/730-git-access.md`](../../../docs/700-security/730-git-access.md) — Git-доступ.
- [`../../../docs/700-security/750-access-contract.md`](../../../docs/700-security/750-access-contract.md) — единый контракт прав.
