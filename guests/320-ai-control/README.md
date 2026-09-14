# 320 — ai-control (bootstrap)

`320-ai-control` — текущий временный bootstrap-узел AI-управления инфраструктурой. Целевой узел — `301-ai-control`.

## Роль

На 320 отрабатываются Hermes/другие агенты, Proxmox MCP, SSH-доступ и модель управления инфраструктурой до готовности целевого контура.

Целевая схема:

```text
AI-агент
├── Proxmox MCP → lifecycle VM/LXC, snapshots, backups
├── Ansible на 311-dev-services → повторяемый deploy по SSH
└── прямой SSH → bootstrap, диагностика и аварийные действия
```

Ansible и Semaphore не размещаются здесь как постоянные компоненты. Они принадлежат `311-dev-services`.

## Файлы

Внутри `rootfs/` хранятся только файлы самого AI-контроля:

```text
rootfs/opt/ai-control/agents/
rootfs/opt/ai-control/mcp/
rootfs/opt/ai-control/interfaces/
```

Конфигурации других VM/LXC остаются в каталогах соответствующих гостей и применяются Ansible из `311-dev-services`.

## SSH

Единый административный пользователь Debian-инфраструктуры — `ops`. Он используется для ручного администрирования, Ansible и автоматизированного SSH-доступа с разными ключами по необходимости.

Прямой SSH из AI Control сохраняется для bootstrap, диагностики, разовых операций и аварийного восстановления.

## Переход на 301

```text
зафиксировать рабочую конфигурацию 320 в Git
→ развернуть 311-dev-services
→ создать 301-ai-control
→ применить конфигурацию 301
→ проверить Hermes, Proxmox MCP, SSH и Ansible через 311
→ назначить 301 основным control plane
→ вывести 320 из эксплуатации отдельным решением
```

320 не переименовывается и не удаляется заранее.

## Защита

- Git — источник истины для повторяемой конфигурации;
- snapshots/backup перед рискованными изменениями;
- отдельный backup persistent data;
- логирование действий;
- проверка health/status/logs после изменений;
- секреты и private keys в Git не хранятся.

Подробная целевая модель AI: [`../../docs/50-ai-control.md`](../../docs/50-ai-control.md). Решение по deploy: [`../311-dev-services/decisions/001-deployment-tooling.md`](../311-dev-services/decisions/001-deployment-tooling.md).
