# Шаблон Debian 13 `9000`

**Тип:** Обзор  
**Статус:** Действующий  
**Основной источник:** Нет — полная техническая спецификация находится в [`build-policy.md`](build-policy.md).

## Текущая базовая версия

```text
VMID: 9000
Имя: tpl-debian13
ОС: Debian 13 (Trixie)
Template-Version: 7
Клонирование: Full Clone
Protection: 1
Административный пользователь: root
SSH: только открытый ключ
```

Шаблон `9000` — универсальная база для управляемых Debian VM. Он не содержит программ конкретного приложения, каталогов будущих сервисов, личных ключей или других учётных данных конкретного потребителя.

## Что гарантирует шаблон

Коротко:

```text
универсальный облачный образ Debian 13
→ проверка SHA-512
→ актуальные пакеты Debian на момент сборки
→ обычное ядро linux-image-amd64
→ QEMU Guest Agent
→ Cloud-Init
→ пароль root заблокирован
→ SSH root только по открытому ключу
→ VGA/noVNC + последовательная консоль
→ очистка данных, уникальных для VM-сборщика
→ готовность к Full Clone
→ protection=1
```

После клонирования конкретная гостевая система получает CPU, RAM, диск, сеть и необходимые **открытые** SSH-ключи до первого запуска.

Точные параметры оборудования, Cloud-Init, ядра, консоли, очистки, проверки полного клона, поведения при ошибке и сведений о происхождении сборки определены только в [`build-policy.md`](build-policy.md).

## Файлы шаблона

```text
templates/debian13/
├── README.md                  # этот паспорт
├── build-policy.md            # основная техническая спецификация
├── cloud-init.yaml            # базовая конфигурация Cloud-Init
├── template-bootstrap.sh      # настройка внутри VM-сборщика
└── template-finalize.sh       # очистка и подготовка к финализации
```

Контур сборки на стороне PVE:

```text
scripts/pve/setup/lib/60-template-contract.sh
scripts/pve/setup/lib/61-template-source.sh
scripts/pve/setup/lib/62-template-build.sh
scripts/pve/setup/lib/63-template-smoke.sh
```

Основной генератор Cloud-Init:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Создание шаблона является частью PVE Configuration; отдельного `create-template.sh` нет.

## Сборка и проверка полного клона

Новая сборка `9000` выполняется штатным контуром PVE Configuration. После новой сборки проверка реального Full Clone запускается автоматически.

Для явной проверки уже существующего шаблона:

```bash
configure-pve.sh --smoke-test-template
```

или через Public Bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --smoke-test-template
```

Проверочная VM использует VMID `9099`. При успешной проверке она удаляется; при ошибке остаётся для диагностики. Подробные правила безопасности и состояния находятся в [`build-policy.md`](build-policy.md).

## Учётные данные

Шаблон не содержит административных учётных данных:

```text
пароль root заблокирован
/root/.ssh отсутствует перед финализацией
список административных authorized_keys пуст
закрытые ключи отсутствуют
SSH host keys удалены перед финализацией
```

Каждый клон получает собственный набор открытых ключей отдельно. Общая модель SSH-ключей PVE, AI, Ansible и человека описана в [`../../docs/23-security.md`](../../docs/23-security.md), а начальная установка ключей в VM/LXC — в [`../../docs/33-guest-bootstrap-and-provisioning.md`](../../docs/33-guest-bootstrap-and-provisioning.md).

## Файловая структура приложений

Шаблон не создаёт заранее каталоги будущих сервисов и не задаёт структуру конкретных приложений.

Общие правила размещения `/opt`, `/etc`, `/var/lib`, `/srv`, журналов, кэша и файлов текущего запуска находятся в [`../../docs/34-linux-filesystem-layout.md`](../../docs/34-linux-filesystem-layout.md).

## Где искать детали

| Вопрос | Основной документ |
|---|---|
| Как строится и проверяется `9000` | [`build-policy.md`](build-policy.md) |
| SSH-ключи и секреты | [`../../docs/23-security.md`](../../docs/23-security.md) |
| Начальный SSH-доступ VM/LXC и передача настройки Ansible | [`../../docs/33-guest-bootstrap-and-provisioning.md`](../../docs/33-guest-bootstrap-and-provisioning.md) |
| Файловая структура сервисов | [`../../docs/34-linux-filesystem-layout.md`](../../docs/34-linux-filesystem-layout.md) |
| Роли и ACL PVE для клонирования шаблона | [`../../docs/25-pve-access-control.md`](../../docs/25-pve-access-control.md) |

Главный принцип: **`README.md` отвечает на вопрос «что такое шаблон `9000` и куда смотреть дальше», а `build-policy.md` является единственной подробной спецификацией его сборки и проверки.**
