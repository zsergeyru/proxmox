# Обзор PVE-хоста

**Тип:** обзор  
**Статус:** проектируется  
**Назначение:** описать роль PVE-хоста после переноса постоянного развёртывания в `910 infra-deployer`.

## 1. Роль PVE

PVE остаётся платформой виртуализации и владельцем фактического состояния VM/LXC, хранилищ, сетей и ACL Proxmox.

Проектная логика на самом PVE минимальна:

```text
чистый PVE
→ public bootstrap
→ 910 infra-deployer
→ закрытый проект внутри 910
→ остальная инфраструктура
```

## 2. Что остаётся на PVE

Обязательны только:

- штатная установка Proxmox VE;
- `vmbr0`;
- `local` и `local-lvm`;
- Debian 13 LXC template для создания 910;
- LXC `910 infra-deployer`;
- pool `managed`;
- ограниченный API token `root@pam!infra-deployer` и его ACL;
- временная блокировка bootstrap во время запуска.

На PVE не требуются:

- постоянная закрытая Git-копия проекта;
- GitHub Deploy Key проекта;
- Docker, Semaphore, OpenTofu, Ansible или Packer;
- `pvedeploy`;
- `deploy-guest`;
- `sync-management-keys`;
- отдельный механизм PVE Configuration;
- отдельное проектное состояние конфигурации PVE.

## 3. Роль public bootstrap

`bootstrap-pve.sh` — единственный проектный сценарий, который выполняется непосредственно на PVE от `root`.

Он отвечает только за минимальную основу, необходимую для запуска 910:

```text
виртуальный объект 910
+ PVE CA
+ ограниченный PVE API token
+ GitHub hand-off
+ запуск private setup
```

Он не управляет обычными гостями и не содержит внутреннюю конфигурацию 910.

## 4. Роль 910

`910 infra-deployer` — постоянный специальный LXC, который не входит в собственное состояние OpenTofu и находится вне pool `managed`.

Именно внутри 910 находятся:

- закрытый Git-репозиторий;
- Semaphore;
- Runner;
- OpenTofu;
- Ansible;
- Packer;
- инфраструктурные секреты и состояние.

## 5. Обычные гости

Обычные VM/LXC описываются в `guests/` и управляются из 910.

Изменяющие PVE-права разворачивателя ограничиваются pool `managed`.

Права на будущие функции, включая клонирование VM template `9000`, не выдаются до появления их реализации.

## 6. Безопасность

910 не получает root SSH на PVE.

Для HTTPS API используется:

```text
root@pam!infra-deployer
```

с `privsep=1` и собственными ограниченными ACL.

Полные права `root@pam` через этот token не передаются.

## 7. Жизненный цикл

```text
установка PVE
→ public bootstrap
→ создание 910
→ автоматическая внутренняя настройка 910
→ штатная эксплуатация из 910
```

Единственное ручное действие первого запуска — добавление read-only GitHub Deploy Key.

## 8. Связанные документы

- [`210-host-bootstrap.md`](210-host-bootstrap.md) — первоначальная подготовка.
- [`220-host-configuration.md`](220-host-configuration.md) — минимальное состояние PVE.
- [`230-host-layout.md`](230-host-layout.md) — локальная структура PVE.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — паспорт 910.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — доступ 910 к PVE.
