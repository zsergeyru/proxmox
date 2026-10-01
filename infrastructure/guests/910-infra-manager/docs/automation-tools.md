# Инструменты управления на 910 infra-manager

**Тип:** локальная спецификация набора инструментов
**Статус:** целевое состояние
**Назначение:** описать OpenTofu, Ansible, Packer, Git и Python внутри `infra-runtime`: какие задачи они выполняют, какие данные используют, как должны взаимодействовать и как восстановить рабочий набор.

Эти инструменты не являются самостоятельными постоянно работающими службами, поэтому описываются в одном документе.

## 1. Общая рабочая цепочка

### Основной поток

Для управляемого Linux-гостя:

```text
Git
 ↓
guest.yaml + provision.yaml + access.yaml
 ↓
OpenTofu
 ↓
VM/LXC в Proxmox
 ↓
Ansible
 ↓
ОС, службы и доступы гостя
```

Packer отдельно собирает базовый VM-шаблон.

Python связывает эти инструменты, выполняет проверки и операции через API, но не является вторым самостоятельным источником требуемого состояния.

### Размещение

Инструменты запускаются внутри:

```text
infra-runtime
```

Проверка:

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime packer version
docker exec infra-runtime ansible --version
docker exec infra-runtime python3 --version
docker exec infra-runtime git --version
```

Версии задаются в [`../provision.yaml`](../provision.yaml).

## 2. OpenTofu

### Исходники и входные данные

Конфигурация:

```text
automation/opentofu/
├── main.tf
├── provider.tf
├── variables.tf
├── versions.tf
└── .terraform.lock.hcl
```

Перед планированием должен создаваться:

```text
/var/lib/infra-manager/opentofu/guests.json
```

из проектных YAML.

`guests.json` является воспроизводимым входом, а не самостоятельным источником требований.

### State

Постоянный state:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

физически:

```text
/mnt/persistent-state/opentofu/state/proxmox.tfstate
```

Потерянный state нельзя заменять пустым поверх существующей инфраструктуры.

### Plan

Задание `OpenTofu Plan` должно:

1. убедиться, что не идёт активация нового `infra-runtime`;
2. построить `guests.json`;
3. выполнить `tofu init` с постоянным backend;
4. выполнить общий `tofu plan`;
5. различить отсутствие изменений, наличие изменений и ошибку.

Наличие изменений само по себе не является ошибкой.

### Применение одного гостя

`Deploy Guest <VMID>` должен:

1. сформировать план только выбранного ресурса;
2. проверить JSON plan;
3. убедиться, что посторонние ресурсы не меняются;
4. применить именно проверенный plan;
5. удалить временный plan после завершения.

910 не должен входить в постоянный OpenTofu state, которым он сам управляет.

## 3. Ansible

### Основной playbook

Общий playbook:

```text
automation/ansible/playbooks/configure-guest.yml
```

Для 910 он подключает роль:

```text
automation/ansible/roles/infra_manager/
```

Ansible должен оставаться владельцем настройки Linux:

- пакеты;
- каталоги;
- файлы;
- Compose;
- systemd;
- SSH trust;
- локальные параметры служб.

### Инвентарь

Постоянный inventory для отдельного запуска не требуется.

`deploy-guest` должен определить адрес гостя и сформировать временный inventory.

### Административный SSH

Целевая схема:

```text
временная Ed25519-пара задания
        ↓
OpenBao ssh-client-signer
        ↓
короткоживущий client certificate
        ↓
root SSH в управляемый Linux-гость
```

Постоянный `guest_ed25519` не является частью конечной схемы и после завершения перехода должен быть удалён из постоянного состояния 910.

Проверка сервера должна выполняться через host CA. Автоматическое отключение `StrictHostKeyChecking` запрещено.

## 4. Packer

### Исходники

```text
automation/packer/9000/
├── template.pkr.hcl
├── variables.pkr.hcl
├── http/preseed.cfg
└── scripts/
```

Целевой шаблон:

```text
VMID 9000
tpl-debian13
```

### Сборка

Задание `Build Template 9000` должно:

1. принимать только VMID 9000;
2. получать PVE credential из рабочего защищённого контура;
3. передавать secrets через `PKR_VAR_*`, а не аргументы процесса;
4. выполнять `packer init`;
5. выполнять `packer validate`;
6. выполнять `packer build`;
7. выполнять пост-проверку шаблона.

Для полной проверки допускается временная VM:

```text
VMID 9099
```

Она должна быть удалена после проверки только если автоматизация уверена, что объект принадлежит текущему тесту.

### Установочный HTTP

Так как `infra-runtime` использует host network, временный HTTP-сервер Packer доступен по адресу 910 без отдельного Docker port mapping.

## 5. Git и Python

### Git

На уровне 910 должна существовать воспроизводимая рабочая копия:

```text
/var/lib/infra-manager/bootstrap-repo/
```

Она может обновляться через:

```text
git fetch --depth 1 origin <branch>
git reset --hard FETCH_HEAD
git clean -ffdx
```

с отдельным read-only Git credential и строгой проверкой сервера.

Semaphore также клонирует проект для заданий, используя тот же логический read-only доступ, но собственный рабочий каталог.

### Python

Исходник:

```text
scripts/infra-manager/infra_manager/
```

Установленный пакет:

```text
/usr/local/lib/infra-manager/infra_manager/
```

Python должен выполнять только программно сложные операции:

- разбор машинных описаний;
- PVE API;
- OpenTofu orchestration;
- запуск Ansible;
- Packer lifecycle;
- Semaphore API;
- OpenBao API через доверенные роли;
- SSH certificate/OTP orchestration;
- проверки состояния.

Python не должен содержать независимую вторую копию полного требуемого состояния 910.

## 6. Задания, проверка и жизненный цикл

### Целевые задания

Набор инструментов должен поддерживать задания Semaphore:

| Задание | Основные инструменты |
|---|---|
| `OpenTofu Plan` | OpenTofu + Python |
| `Build Template 9000` | Packer + Python |
| `Deploy Guest <VMID>` | OpenTofu + Ansible + Python |
| `Deploy Guest 910` | Ansible + безопасная активация runtime |
| `Sync SSH Access` | OpenBao + Ansible + `access.yaml` |
| `Initialize OpenBao 910` | OpenBao + PVE-only helper + Python |

Старое `Sync Machine SSH` не входит в целевой набор.

### Режимы deploy-guest

Служебные режимы могут разделять:

```text
создание инфраструктуры
базовую настройку
полную настройку
настройку уже существующего объекта
```

Обычный пользовательский путь должен оставаться единым заданием `Deploy Guest <VMID>`.

Для 910 обычное обновление должно определяться как самообновление и использовать отложенную активацию `infra-runtime`.

### Проверка

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime packer version
docker exec infra-runtime ansible --version
docker exec infra-runtime python3 -c 'import proxmoxer, yaml, jsonschema'
docker exec infra-runtime git --version
infra-manager-status --full
```

Дополнительно нужно подтверждать:

- доступность OpenTofu state;
- отсутствие постоянного Ansible private key в целевом состоянии;
- выпуск короткоживущего client certificate;
- проверку host CA;
- успешную OTP-синхронизацию из `access.yaml`.

### Обновление

Версии OpenTofu и Packer меняются в `provision.yaml`.

Python-зависимости меняются согласованно в:

```text
provision.yaml
rootfs/opt/infra-manager/compose/runtime/requirements.txt
```

Системные пакеты образа — в:

```text
rootfs/opt/infra-manager/compose/runtime/Dockerfile
```

После изменения выполняется `Deploy Guest 910`. Ручная установка новых бинарников внутрь текущего контейнера не считается обновлением.

### Восстановление

После потери `infra-runtime` нужно:

1. восстановить `state/opentofu`;
2. восстановить доступы и OpenBao;
3. получить проект;
4. собрать `infra-runtime`;
5. проверить версии;
6. проверить OpenTofu state;
7. синхронизировать Semaphore;
8. синхронизировать SSH OTP-контракт;
9. выполнить `OpenTofu Plan`;
10. выполнить полный status.

Если инфраструктура существует, а ожидаемый прежний OpenTofu state отсутствует, `apply` запрещён до выяснения причины.

## 7. Источники и связанные документы

Машинные источники:

- [`../provision.yaml`](../provision.yaml);
- [`../rootfs/opt/infra-manager/compose/runtime/Dockerfile`](../rootfs/opt/infra-manager/compose/runtime/Dockerfile);
- [`../rootfs/opt/infra-manager/compose/runtime/requirements.txt`](../rootfs/opt/infra-manager/compose/runtime/requirements.txt);
- `automation/opentofu/`;
- `automation/ansible/`;
- `automation/packer/`;
- `scripts/infra-manager/`.

Связанные документы:

- [`infra-runtime.md`](infra-runtime.md) — среда выполнения;
- [`semaphore.md`](semaphore.md) — задания;
- [`openbao.md`](openbao.md) — SSH CA и OTP;
- [`data-and-access.md`](data-and-access.md) — OpenTofu state и доступы;
- [`system-services.md`](system-services.md) — установка команд;
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — переходные расхождения текущей реализации.
