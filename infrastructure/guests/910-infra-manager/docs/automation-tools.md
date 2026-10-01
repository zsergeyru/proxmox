# Инструменты управления на 910 infra-manager

**Назначение:** практическое руководство по OpenTofu, Ansible, Packer, Git и Python внутри `infra-runtime`: где они находятся, какие данные используют, как запускаются и как их проверять.

Эти инструменты не являются отдельными постоянно работающими службами. Они используются заданиями Semaphore и служебным кодом 910.

## 1. Общая рабочая цепочка

Для обычного Linux-гостя схема такая:

```text
Git
 ↓
guest.yaml + provision.yaml
 ↓
OpenTofu
 ↓
VM/LXC в Proxmox
 ↓
Ansible
 ↓
ОС и службы внутри гостя
```

Packer используется отдельно для сборки базового шаблона VM.

Python связывает эти части и проверяет условия, которые неудобно или опасно выражать только shell-командами.

## 2. Где находятся инструменты

Все основные инструменты запускаются внутри контейнера:

```text
infra-runtime
```

Проверить версии:

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime packer version
docker exec infra-runtime ansible --version
docker exec infra-runtime python3 --version
docker exec infra-runtime git --version
```

Текущие версии OpenTofu и Packer задаются в [`../provision.yaml`](../provision.yaml).

## 3. OpenTofu

OpenTofu управляет объектами Proxmox, которые принадлежат постоянному управляющему контуру.

### 3.1. Конфигурация проекта

Исходники:

```text
automation/opentofu/
├── main.tf
├── provider.tf
├── variables.tf
├── versions.tf
└── .terraform.lock.hcl
```

Рабочий код 910 не копирует эти файлы в отдельный постоянный каталог: задания используют версию из клонированного репозитория Semaphore.

### 3.2. Входной файл гостей

Перед работой OpenTofu Python создаёт:

```text
/var/lib/infra-manager/opentofu/guests.json
```

Генератор:

```text
scripts/guests/render-opentofu-input.py
```

Для постоянного контура 910 файл создаётся с исключением VMID `910`.

Это воспроизводимый файл. Источник требований — проектные YAML, а не `guests.json`.

Проверить:

```bash
test -s /var/lib/infra-manager/opentofu/guests.json
jq . /var/lib/infra-manager/opentofu/guests.json
```

### 3.3. Состояние

Основной state:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Физически:

```text
/mnt/persistent-state/opentofu/state/proxmox.tfstate
```

Backend инициализируется локальным путём к этому файлу.

Проверить наличие:

```bash
ls -lh /var/lib/infra-manager/opentofu/state/
```

Потерянный state нельзя молча заменять новым пустым state поверх существующей инфраструктуры.

### 3.4. Задание OpenTofu Plan

Semaphore запускает:

```text
scripts/infra-manager/jobs/opentofu-plan.py
```

Он:

1. проверяет, что не идёт активация нового `infra-runtime`;
2. строит `guests.json`;
3. выполняет `tofu init` с постоянным backend;
4. выполняет общий `tofu plan`;
5. различает `нет изменений`, `есть изменения` и ошибку.

Наличие изменений не считается ошибкой.

### 3.5. Применение одного гостя

`Deploy Guest <VMID>` не выполняет безусловный общий `tofu apply`.

Код:

1. создаёт план только для выбранного гостя;
2. показывает план в JSON;
3. убеждается, что изменения не затрагивают посторонние ресурсы;
4. применяет проверенный plan;
5. удаляет временный `.tfplan` после завершения.

Для VMID `910` действует особый путь: объект Proxmox сверяется как существующий, но не включается в постоянный state самого 910.

## 4. Ansible

Ansible настраивает Linux после создания или проверки объекта Proxmox.

### 4.1. Основной playbook

```text
automation/ansible/playbooks/configure-guest.yml
```

Он используется и для обычных Linux-гостей, и для 910.

Для 910 включается роль:

```text
automation/ansible/roles/infra_manager/
```

Порядок её частей:

```text
persistence
→ pve_access
→ ansible_access
→ repository
→ semaphore
→ runtime
→ openbao
→ recovery
→ verify
```

### 4.2. Инвентарь

Постоянный inventory не хранится.

`deploy-guest` определяет адрес выбранного гостя и формирует временный inventory на время одного запуска.

### 4.3. SSH-доступ

Текущая реализация может использовать переходный постоянный ключ:

```text
/etc/infra-manager/ansible/guest_ed25519
```

При штатном административном доступе код умеет получать короткоживущий сертификат Ansible через OpenBao.

Host key checking всегда включён.

### 4.4. Ручная проверка Ansible

Проверить сам инструмент:

```bash
docker exec infra-runtime ansible --version
```

Проверить ключ внутри контейнера:

```bash
docker exec infra-runtime test -s /etc/infra-manager/ansible/guest_ed25519
```

Не следует вручную запускать `configure-guest.yml` без тех переменных, которые готовит `deploy-guest.py`, если цель — штатное развёртывание. Основной пользовательский путь — задание `Deploy Guest <VMID>`.

## 5. Packer

Packer собирает базовый Debian-шаблон.

### 5.1. Исходники

```text
automation/packer/9000/
├── template.pkr.hcl
├── variables.pkr.hcl
├── http/preseed.cfg
└── scripts/
```

### 5.2. Задание

Semaphore:

```text
Build Template 9000
```

Точка входа:

```text
scripts/infra-manager/jobs/build-template.py 9000
```

Основная логика находится в:

```text
scripts/infra-manager/infra_manager/template_build.py
```

### 5.3. Порядок сборки

Код:

1. принимает только VMID `9000`;
2. получает PVE-доступ из тех же рабочих переменных, что OpenTofu;
3. передаёт секреты Packer через переменные окружения `PKR_VAR_*`, а не через аргументы командной строки;
4. выполняет `packer init`;
5. выполняет `packer validate`;
6. выполняет `packer build`;
7. приводит шаблон к финальным требованиям проекта;
8. проверяет результат.

Текущий целевой шаблон:

```text
VMID 9000
tpl-debian13
```

### 5.4. Проверочная VM

Для полной проверки используется временная копия:

```text
VMID 9099
```

Она создаётся только как проверочный объект и после проверки удаляется.

Неизвестный объект с VMID 9000 или 9099 не должен удаляться автоматически только ради продолжения сборки.

### 5.5. HTTP во время установки

Так как `infra-runtime` использует `network_mode: host`, временный HTTP-сервер Packer работает на сетевом адресе 910 и доступен устанавливаемой VM без отдельного Docker port mapping.

## 6. Git

Git нужен для двух разных задач.

### 6.1. Рабочая копия самого 910

На уровне Debian 910 находится:

```text
/var/lib/infra-manager/bootstrap-repo/
```

Она используется для установки локальных файлов и Python-кода.

При самообновлении `repository.yml`:

```text
git fetch --depth 1 origin <branch>
git reset --hard FETCH_HEAD
git clean -ffdx
```

с использованием строгой SSH-конфигурации.

Эта рабочая копия воспроизводима из Git.

### 6.2. Репозиторий внутри Semaphore

Semaphore отдельно клонирует:

```text
git@github.com:zsergeyru/proxmox.git
```

для каждого задания.

Он использует read-only Deploy Key из `/run/infra-manager/secrets/` и постоянный `known_hosts` в каталоге Semaphore.

## 7. Python-код 910

Исходник:

```text
scripts/infra-manager/infra_manager/
```

Ansible устанавливает его в:

```text
/usr/local/lib/infra-manager/infra_manager/
```

Команды 910 запускают этот пакет через `PYTHONPATH=/usr/local/lib/infra-manager` или через shell-обёртки.

### 7.1. Основные задачи Python

Python выполняет:

- разбор `guest.yaml` и `provision.yaml`;
- формирование `guests.json`;
- работу с PVE API;
- планирование и применение OpenTofu;
- запуск Ansible;
- сборку и проверку Packer-шаблона;
- синхронизацию Semaphore;
- работу с OpenBao через PVE-only helper;
- выдачу и проверку SSH-сертификатов;
- итоговую проверку 910.

Машинные требования при этом остаются в YAML и других проектных файлах.

## 8. Задания Semaphore и соответствующие инструменты

| Задание | Основной инструмент | Точка входа |
|---|---|---|
| `OpenTofu Plan` | OpenTofu | `jobs/opentofu-plan.py` |
| `Build Template 9000` | Packer | `jobs/build-template.py 9000` |
| `Deploy Guest 410` | OpenTofu + Ansible | `jobs/deploy-guest.py 410` |
| `Deploy Guest 910` | Ansible + специальное самообновление | `jobs/deploy-guest.py 910` |
| `Sync Machine SSH` | Python + OpenBao | `jobs/sync-machine-ssh.py` |
| `Initialize OpenBao 910` | Python + PVE helper + OpenBao | `jobs/initialize-openbao.py` |

Все точки входа сначала проверяют, что не идёт конфликтующая активация нового `infra-runtime`.

## 9. Режимы deploy-guest.py

Скрипт поддерживает несколько служебных режимов:

```text
--bootstrap-scope
--infrastructure-only
--provision-base-only
--provision-only
--provision-existing-only
```

Обычные задания Semaphore `Deploy Guest 410/910` вызывают стандартный путь без этих флагов.

Флаги используются первоначальным контуром и восстановительными сценариями, когда нужно разделить создание объекта Proxmox и настройку ОС.

Для VMID `910` обычный полный запуск без `--bootstrap-scope` определяется как самообновление и резервирует отложенную активацию `infra-runtime`.

## 10. Проверка рабочего набора инструментов

Штатная проверка из `verify.yml`:

```bash
docker exec infra-runtime tofu version
docker exec infra-runtime packer version
docker exec infra-runtime ansible --version
docker exec infra-runtime python3 -c 'import proxmoxer, yaml, jsonschema'
```

Дополнительно:

```bash
docker exec infra-runtime git --version
docker exec infra-runtime test -s /etc/infra-manager/ansible/guest_ed25519
```

Полный итог:

```bash
infra-manager-status --full
```

## 11. Обновление инструментов

Версии OpenTofu и Packer меняются в `provision.yaml`.

Python-зависимости меняются согласованно в:

```text
provision.yaml
rootfs/opt/infra-manager/compose/runtime/requirements.txt
```

Системные пакеты образа меняются в:

```text
rootfs/opt/infra-manager/compose/runtime/Dockerfile
```

После изменения выполняется `Deploy Guest 910`: новый образ собирается и безопасно активируется после завершения текущего задания.

Не следует вручную заменять `tofu`, `packer` или Python-пакеты внутри работающего контейнера.

## 12. Что является постоянным

Постоянно сохраняются:

```text
/mnt/persistent-state/opentofu/state/
/mnt/persistent-state/ansible/
```

Воспроизводятся из Git и образа:

```text
OpenTofu binary
Packer binary
Ansible
Python libraries
automation/opentofu/
automation/packer/
/var/lib/infra-manager/bootstrap-repo/
/var/lib/infra-manager/opentofu/guests.json
infra-runtime image
```

## 13. Восстановление инструментов после потери контейнера

Если `infra-runtime` потерян:

1. восстановить постоянные `state/opentofu` и `state/ansible`;
2. получить проект в `/var/lib/infra-manager/bootstrap-repo`;
3. установить Compose-файлы из `rootfs`;
4. собрать `infra-runtime`;
5. проверить версии инструментов;
6. проверить наличие OpenTofu state;
7. не выполнять `apply`, если ожидаемый старый state отсутствует;
8. синхронизировать Semaphore;
9. выполнить `OpenTofu Plan`;
10. выполнить `infra-manager-status --full`.

## 14. Типичные неисправности

### 14.1. OpenTofu сообщает о пустом state

Сначала проверить:

```bash
ls -lh /mnt/persistent-state/opentofu/state/
readlink -f /var/lib/infra-manager/opentofu
```

Если инфраструктура существует, а прежний state ожидался, не продолжать автоматическое применение до выяснения причины.

### 14.2. Ansible не может войти в гость

Проверить:

- наличие `/etc/infra-manager/ansible/guest_ed25519`;
- права файла;
- SSH host certificate или `known_hosts` для цели;
- состояние OpenBao, если используется краткоживущий сертификат.

### 14.3. Packer не видит установочный HTTP

Проверить, что `infra-runtime` действительно работает в `network_mode: host` и что устанавливаемая VM может достичь IP 910.

### 14.4. Python-модуль не импортируется на самом 910

Проверить:

```bash
test -d /usr/local/lib/infra-manager/infra_manager
PYTHONPATH=/usr/local/lib/infra-manager python3 -c 'import infra_manager'
```

Если каталог отсутствует, повторно применить роль `infra_manager`.

## 15. Связанные документы

- [`../provision.yaml`](../provision.yaml) — версии и постоянные пути.
- [`infra-runtime.md`](infra-runtime.md) — образ и подключённые каталоги.
- [`semaphore.md`](semaphore.md) — задания, которые запускают инструменты.
- [`data-and-access.md`](data-and-access.md) — OpenTofu state и Ansible identity.
- [`system-services.md`](system-services.md) — установка Python-кода и команд.
- [`../../../../automation/opentofu/`](../../../../automation/opentofu/) — конфигурация OpenTofu.
- [`../../../../automation/ansible/playbooks/configure-guest.yml`](../../../../automation/ansible/playbooks/configure-guest.yml) — общий playbook.
- [`../../../../automation/packer/9000/`](../../../../automation/packer/9000/) — шаблон Packer 9000.
- [`../../../../scripts/infra-manager/jobs/`](../../../../scripts/infra-manager/jobs/) — точки входа Semaphore.
- [`../../../../scripts/infra-manager/infra_manager/opentofu.py`](../../../../scripts/infra-manager/infra_manager/opentofu.py) — рабочая область OpenTofu.
- [`../../../../scripts/infra-manager/infra_manager/guest_deploy.py`](../../../../scripts/infra-manager/infra_manager/guest_deploy.py) — развёртывание гостя.
- [`../../../../scripts/infra-manager/infra_manager/template_build.py`](../../../../scripts/infra-manager/infra_manager/template_build.py) — сборка шаблона.