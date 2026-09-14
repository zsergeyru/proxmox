# AI Control — статус архитектуры

## Статус

Архитектура `301-ai-control` **отправлена на перепроектирование** вместе с bootstrap/deploy workflow.

Предыдущая схема с Proximo, Docker, отдельными public bootstrap stages и Hermes сохранена в Git history, но больше не считается канонической.

## Что остаётся зафиксировано

Пока сохраняются только общие инфраструктурные намерения:

```text
301 → ai-control
320 → старый bootstrap/legacy control node до отдельного решения
9000 → tpl-debian13
```

`301` не должен автоматически попадать в обычную self-managed write-zone. Любые права AI на PVE должны снова пройти отдельное проектирование и проверку.

Приватные keys, API token secrets и provider credentials по-прежнему не должны храниться в Git.

## Что больше не считаем принятым

До нового решения не считать каноническими:

- Proximo как обязательный MCP для `301`;
- прежнюю PVE user/token/ACL схему;
- Docker как обязательную часть control plane;
- прежнюю структуру `/opt/ai-control/`;
- Hermes как обязательный или первый агент;
- прежнюю схему GitHub Deploy Key;
- прямой bootstrap `301` специальным public script;
- прежнюю цепочку `create-ai-control-vm → prepare-ai-control → install-ai-agent`;
- старую границу ответственности между `301`, `311` и Ansible.

Все эти решения можно использовать как материал для сравнения, но новая архитектура должна быть выведена заново из требований.

## Что нужно решить заново

Новая версия документа должна ответить как минимум на вопросы:

1. Для чего именно нужен `301` и какие действия он должен выполнять.
2. Должен ли AI управлять PVE напрямую, через ограниченный API/MCP или через другой control service.
3. Где проходит hard security boundary.
4. Как создаются VM/LXC без зависимости от AI.
5. Как AI получает SSH/Git access к уже созданным гостям.
6. Где живут repeatable OS/application configuration и orchestration.
7. Как устроены backup, recovery, token/key rotation и audit.
8. Как менять AI agent без перестройки всей инфраструктуры.

## Активная часть

Единственный поддерживаемый public bootstrap-компонент сейчас — создание base template:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
→ 9000 tpl-debian13
```

AI Control будет проектироваться поверх этого заново.

См. также:

- [`31-bootstrap.md`](31-bootstrap.md);
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md).
