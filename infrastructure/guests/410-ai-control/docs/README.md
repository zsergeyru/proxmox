# Документация 410 ai-control

Этот каталог содержит локальную документацию гостя `410 ai-control`.

AI Control здесь означает **всю систему 410**, а не отдельную программу.

## Документы

| Файл | Для чего открывать |
|---|---|
| [`ai-control.md`](ai-control.md) | состав системы 410, границы, каталоги, сеть, развёртывание и восстановление |
| [`hermes.md`](hermes.md) | Hermes как основной агент и агентная среда |
| [`open-webui.md`](open-webui.md) | Open WebUI как основной пользовательский интерфейс |
| [`implementation-status.md`](implementation-status.md) | что уже реализовано и что ещё требуется |
| [`decisions.md`](decisions.md) | причины локальных архитектурных решений |

## Порядок развёртывания

После создания Debian 13 VM:

1. применить базовую настройку Linux;
2. установить Docker;
3. создать каталоги постоянного состояния;
4. создать локальные секреты Hermes и Open WebUI;
5. сформировать и запустить Compose;
6. проверить `/health` Hermes и Open WebUI;
7. настроить поставщика модели Hermes;
8. проверить полный диалог Open WebUI → Hermes → модель;
9. подключать внешние инструменты по одному;
10. сверить `implementation-status.md`.

## Где находится реализация

| Источник | Что определяет |
|---|---|
| [`../guest.yaml`](../guest.yaml) | параметры VM и системная роль |
| [`../provision.yaml`](../provision.yaml) | Docker, Hermes, Open WebUI, каталоги, версии и постоянные данные |
| [`../../../../automation/ansible/playbooks/configure-guest.yml`](../../../../automation/ansible/playbooks/configure-guest.yml) | общий порядок настройки |
| [`../../../../automation/ansible/roles/ai_control/`](../../../../automation/ansible/roles/ai_control/) | развёртывание Hermes и Open WebUI |
| [`../../../security/access.yaml`](../../../security/access.yaml) | внешние машинные права 410 |

## Общая документация проекта

- [`../../../../docs/500-ai/500-overview.md`](../../../../docs/500-ai/500-overview.md) — роль AI в квартире;
- [`../../../../docs/500-ai/510-ai-control.md`](../../../../docs/500-ai/510-ai-control.md) — общая архитектура;
- [`../../../../docs/500-ai/520-ai-management.md`](../../../../docs/500-ai/520-ai-management.md) — правила действий;
- [`../../../../docs/500-ai/590-decisions.md`](../../../../docs/500-ai/590-decisions.md) — общие решения;
- [`../../../../docs/700-security/750-access-contract.md`](../../../../docs/700-security/750-access-contract.md) — контракт полномочий.
