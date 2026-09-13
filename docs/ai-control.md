# AI Control — архитектурные принципы

`ai-control` — отдельная VM управляющего AI-контура. Целевой VMID — `301`; текущая `320-ai-control` сохраняется как bootstrap до развёртывания и проверки новой VM.

## Назначение

AI-агент должен уметь администрировать домашнюю инфраструктуру, а не только собственный контейнер.

Базовое разделение ответственности:

```text
Proxmox MCP
→ создание и жизненный цикл VM/LXC, ресурсы, start/stop/reboot, snapshots и backups

Ansible в 311-dev-services
→ повторяемые изменения внутри гостевых ОС через SSH: пакеты, файлы, systemd, Docker/Compose и приложения

прямой SSH из ai-control
→ bootstrap, диагностика, разовые административные действия и аварийные сценарии

специализированные MCP/API
→ используются там, где дают реальное преимущество
```

Отдельный универсальный `deploy-mcp` не является частью целевой архитектуры.

Решение по Ansible/Semaphore и простой модели `rootfs/` зафиксировано в [`../guests/311-dev-services/decisions/001-deployment-tooling.md`](../guests/311-dev-services/decisions/001-deployment-tooling.md). Исходная простая модель управления bootstrap-узла описана в [`../guests/320-ai-control/decisions/001-simple-management-model.md`](../guests/320-ai-control/decisions/001-simple-management-model.md). Точная матрица разрешённых и запрещённых Proxmox-операций Hermes зафиксирована в [`../guests/320-ai-control/decisions/002-proxmox-permissions.md`](../guests/320-ai-control/decisions/002-proxmox-permissions.md).

## Размещение компонентов

В `ai-control` находятся только компоненты AI-управляющего контура:

- Hermes и другие AI-агенты;
- Proxmox MCP и другие специализированные MCP;
- Git/SSH/API-клиенты, необходимые агентам;
- web/voice интерфейсы к агентам;
- служебный код, относящийся непосредственно к AI-управлению инфраструктурой.

Ansible и Semaphore не размещаются здесь как постоянные компоненты: это DevOps-инструменты `311-dev-services`.

Общие сервисы не должны жить внутри `ai-control` только потому, что ими пользуется Hermes:

- DNS/VPN/PBR → `109-network-gateway`;
- MQTT/Zigbee2MQTT/ESPHome → `211-automation-services`;
- Ansible/Semaphore/Git/CI → `311-dev-services`;
- Homarr и обычные приложения → `321-app-services`;
- STT/TTS → `331-ai-services`;
- monitoring → `401-monitoring`;
- Frigate → `501-frigate`.

## Типовой сценарий управления

Для операции уровня виртуализации Hermes использует Proxmox MCP.

Для повторяемого изменения внутри гостя целевой путь такой:

```text
Hermes
→ подготовить/изменить конфигурацию в Git
→ инициировать Ansible на 311-dev-services
→ Ansible подключается по SSH к нужному гостю
→ применяет конфигурацию
→ проверяется результат
```

Semaphore предназначен прежде всего для ручного запуска тех же Ansible-сценариев человеком и не является обязательным звеном между Hermes и Ansible.

Прямой SSH сохраняется: он нужен для первоначального bootstrap, диагностики, разовых операций и восстановления, когда стандартный deploy-сценарий ещё не готов или недоступен.

## Файлы и Git

Конфигурация самого гостя описывается его `guest.yaml`. Всё, что должно находиться внутри гостевой ОС, хранится под `rootfs/` по реальному абсолютному пути.

`ai-control` не хранит копии `rootfs/` других VM/LXC. Повторяемое применение содержимого `rootfs/` выполняется Ansible из `311-dev-services` по правилам, описанным в [`../guests/README.md`](../guests/README.md).

## Права и модель защиты

Агенту предоставляются достаточные административные права для поставленных задач, но граница проходит между управлением гостями и управлением самим PVE host.

Hermes может полноценно управлять lifecycle выделенных VM/LXC в managed pool: создавать и клонировать, менять guest-level ресурсы, start/stop/reboot, делать snapshots/backups и выполнять другие разрешённые guest-level операции. При этом ему не выдаются права на изменение PVE host network, storage configuration, ACL, пользователей, API tokens, Datacenter/host firewall, SDN, сертификатов, PVE repositories или reboot/shutdown самого гипервизора.

Защищённые объекты `100`, текущий bootstrap `320` и template `9000` на первом этапе не входят в обычную write-зону Hermes; для `9000` требуется read/clone-доступ без права изменения template.

Полная матрица и правила destructive actions определены в [`../guests/320-ai-control/decisions/002-proxmox-permissions.md`](../guests/320-ai-control/decisions/002-proxmox-permissions.md).

Основная защита:

1. Git — источник истины для повторяемой конфигурации и кода.
2. Proxmox snapshots перед рискованными изменениями.
3. Регулярные Proxmox backups.
4. Отдельный backup persistent data и Docker volumes.
5. Резервная копия критичных файлов перед изменением, если это требуется.
6. Логирование действий агента и deploy-задач.
7. Проверка health/status/logs после изменений.
8. Поэтапная замена: сначала создать/проверить новое, затем удалять старое.

Секреты, приватные SSH-ключи, пароли и API tokens в Git не хранятся.

## Текущий bootstrap и переход на 301

`320-ai-control` используется для отработки Hermes, MCP, SSH-доступа и модели управления. Пока `311-dev-services` ещё не развёрнут, bootstrap-операции допустимо выполнять напрямую по SSH.

Переход выполняется поэтапно:

```text
320 bootstrap работает
→ разворачивается инфраструктура, включая 311-dev-services
→ Ansible становится штатным повторяемым deploy-механизмом
→ создаётся 301-ai-control
→ переносится воспроизводимая AI-конфигурация из Git
→ проверяются Hermes, Proxmox MCP, прямой SSH и запуск deploy через 311
→ 301 становится основным control plane
→ 320 выводится из эксплуатации отдельным решением
```

Ansible и Semaphore при этом остаются в `311-dev-services`, а не переносятся в `301-ai-control`.
