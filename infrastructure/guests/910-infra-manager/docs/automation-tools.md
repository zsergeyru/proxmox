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

### Провайдер

Для `bpg/proxmox` используется локальное зеркало:

```text
/var/lib/infra-manager/opentofu/provider-mirror
```

Версия берётся из `.terraform.lock.hcl`. Если подходящий пакет уже есть и его SHA256 входит в `zh:` lock-файла, повторная загрузка не выполняется. Если пакета нет, он скачивается из официального GitHub Releases проекта `bpg/terraform-provider-proxmox`, проверяется по опубликованному SHA256 и дополнительно сверяется с lock-файлом.

`tofu init` запускается с `-plugin-dir` и не должен зависеть от доступности `registry.opentofu.org`. Зеркало является воспроизводимым кэшем и находится внутри постоянного каталога OpenTofu.

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

`Deploy Guest` после выбора гостя должен:

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

Для уже управляемого гостя штатная схема:

```text
временная Ed25519-пара операции
        ↓
OpenBao ssh-client-signer
        ↓
короткоживущий client certificate
        ↓
root SSH в управляемый Linux-гость
```

Для совершенно нового обычного гостя перед этим используется отдельная bootstrap-пара:

```text
одноразовый bootstrap public key
        ↓
OpenTofu / Cloud-Init
        ↓
первый Ansible-вход
        ↓
установка доверия к client CA
        ↓
проверка входа по сертификату OpenBao
        ↓
удаление bootstrap-доступа и локального bootstrap private key
```

Если первичное развёртывание разделено на фазы, bootstrap private key может временно сохраняться в рабочем каталоге OpenTofu до успешного завершения настройки. Постоянного `guest_ed25519` в 910 и Semaphore нет.

Первоначальное создание самого 910 использует отдельный временный bootstrap-контур 990. Этот ключ не становится штатной identity 910.

Перед запросом временного сертификата `deploy-guest` обязан сначала синхронизировать на PVE полный комплект OpenBao helper: исполняемый файл, пакет `openbao_host/` и описание infra-manager. Обновлять один `infra-manager-openbao-unseal` отдельно запрещено, потому что его версия должна совпадать с версией Python-пакета.

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
| `Set Log Level` | Semaphore API + постоянная настройка `INFRA_LOG_LEVEL` |
| `Deploy Guest` | выбор гостя + OpenTofu + Ansible + Python |
| `Status Guest` | PVE + проверки роли |
| `Repair Guest` | PVE + безопасные повторяемые исправления |
| `Test Guest` | PVE + сетевые и ролевые проверки |
| `Sync Guest` | Git-копия задания + обработчик роли |
| `Sync SSH Access` | OpenBao + Python + `access.yaml` |

### Режимы deploy-guest

Служебные режимы могут разделять:

```text
создание инфраструктуры
базовую настройку
полную настройку
настройку уже существующего объекта
```

Обычное полное применение состояния выполняется через `Deploy Guest`. Проверка, безопасное исправление, расширенный тест и лёгкая синхронизация вынесены в отдельные шаблоны `Status Guest`, `Repair Guest`, `Test Guest` и `Sync Guest`. Все они используют тот же машинный каталог и общий Python-диспетчер.

Для 910 обычное обновление должно определяться как самообновление и использовать отложенную активацию `infra-runtime`.

### Проверка

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime packer version
docker exec infra-runtime ansible --version
docker exec infra-runtime python3 -c 'import proxmoxer, yaml, jsonschema'
docker exec infra-runtime git --version
infra-manager status
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

После изменения выполняется единый `Deploy Guest` с выбором `910 infra-manager`. Ручная установка новых бинарников внутрь текущего контейнера не считается обновлением.

### Восстановление

Для обычного сбоя управляющей среды с PVE выполняется:

```bash
infra-manager repair
```

Если 910 потерян или обычное исправление невозможно, используется:

```bash
infra-manager recover
```

Обе операции защищают существующее состояние. Если инфраструктура существует, а ожидаемый прежний OpenTofu state отсутствует, автоматическое восстановление и `apply` запрещены до выяснения причины.

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
