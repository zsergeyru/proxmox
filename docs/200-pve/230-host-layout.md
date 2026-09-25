# Структура PVE-хоста

**Тип:** справочник  
**Статус:** проектируется  
**Назначение:** зафиксировать проектные объекты и временные файлы, которые остаются непосредственно на PVE.

## 1. Основной принцип

На PVE нет постоянной закрытой Git-копии и не работает отдельный проектный движок развёртывания.

Постоянное состояние проекта на физическом PVE должно быть минимальным.

## 2. Временные файлы bootstrap

Во время запуска используются:

```text
/run/lock/proxmox-bootstrap.lock
/run/proxmox-bootstrap/
```

Первый путь блокирует параллельный запуск.

Второй используется только для кратковременной передачи создаваемого PVE API secret и сертификата в 910.

Эти файлы не являются постоянным состоянием.

## 3. Штатные файлы Proxmox

Public bootstrap читает штатное состояние Proxmox через его команды и API.

Из файлов напрямую используется корневой сертификат:

```text
/etc/pve/pve-root-ca.pem
```

Проект не создаёт рядом собственную постоянную файловую структуру управления PVE.

## 4. Постоянные объекты Proxmox

К проектной основе относятся:

```text
LXC 910 infra-manager
pool managed
API token root@pam!infra-manager
ACL этого token
Debian 13 LXC template в local:vztmpl
```

Отдельный пользователь PVE и собственная роль проекта не требуются.

910 находится вне pool `managed`.

## 5. Автоматический unseal OpenBao

После реализации OpenBao на PVE остаются только минимальные файлы, необходимые для его автоматического открытия:

~~~text
/usr/local/sbin/openbao-unseal
/etc/systemd/system/openbao-unseal.service
/etc/proxmox-bootstrap/secrets/openbao-unseal.key
~~~

Назначение:

- `openbao-unseal` — небольшой сценарий ожидания 910 и запуска локальной операции unseal;
- `openbao-unseal.service` — однократное systemd-задание при загрузке PVE;
- `openbao-unseal.key` — ключ обычного автоматического unseal, доступный только root.

Файл ключа имеет владельца `root:root` и режим `0600`.

Исходный код сценария и unit хранится в закрытом проекте. Public bootstrap не должен содержать вторую независимую реализацию логики OpenBao.

Внутри 910 Ansible устанавливает вспомогательную команду:

~~~text
/usr/local/sbin/openbao-unseal-local
~~~

Она работает только с локальным API OpenBao на `127.0.0.1`, читает ключ через стандартный ввод и никогда не сохраняет его в файл внутри 910.

Эти файлы являются исключением из общего правила минимальности PVE: они нужны для разрыва циклической зависимости, потому что сам OpenBao не может хранить ключ собственного запуска.

## 6. Чего на PVE больше нет в целевой схеме

Не являются частью текущего контракта:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/lib/pvedeploy/
/var/log/proxmox-deployer/
/usr/local/sbin/pve-configuration-status
/usr/local/sbin/deploy-guest
/usr/local/sbin/sync-management-keys
```

Также не нужны:

- Linux-пользователь `pvedeploy`;
- GitHub Deploy Key закрытого проекта на PVE;
- локальный OpenTofu state;
- реестр management SSH-ключей на PVE;
- собственные журналы PVE Configuration.

## 7. Где находится инфраструктурное состояние

Постоянные данные разворачивателя находятся внутри 910:

```text
/etc/infra-manager/
/var/lib/infra-manager/
/opt/infra-manager/
```

Там находятся секреты, Semaphore, OpenTofu state и рабочая конфигурация инфраструктурного контура.

Закрытый Git checkout внутри 910 является воспроизводимым и не считается источником уникального состояния.

## 8. Связанные документы

- [`210-host-bootstrap.md`](210-host-bootstrap.md) — создание 910.
- [`220-host-configuration.md`](220-host-configuration.md) — минимальный контракт PVE.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — token и ACL.
- [`../../infrastructure/guests/910-infra-manager/README.md`](../../infrastructure/guests/910-infra-manager/README.md) — постоянные данные 910.
