# Scripts

Повторяемые служебные операции, которые не относятся к одному конкретному гостю.

Скрипты должны быть идемпотентными или явно описывать побочные эффекты. Опасные операции должны иметь проверку и понятный rollback.

## Общий resolver guest-конфигурации

`scripts/guest_config.py` — единая чистая реализация преобразования source-конфигурации гостя в effective desired state.

Она отвечает только за детерминированные правила данных:

```text
defaults
+ profile
+ guest.yaml
+ VMID-addressing / optional IP override
→ effective desired state
```

Модуль не обращается к Proxmox API и ничего не меняет на хосте. `scripts/validate_repo.py` использует его для проверки, а будущий `scripts/pve/deploy-guest.py` должен использовать тот же resolver перед построением PLAN/APPLY.

Правила merge, вычисления management IP и формирования effective network нельзя дублировать отдельной реализацией внутри validator или deployer.

## Bootstrap guests

Будущий `deploy-guest.py` получает ограниченный расширяемый bootstrap-интерфейс после создания/запуска гостя и готовности management-доступа.

Начальный whitelist:

```text
base
git
docker
ansible_controller
```

Реализация должна быть вынесена в отдельные идемпотентные handlers, а не наращиваться внутри ядра deployer. Предпочтительное направление:

```text
scripts/bootstrap/base.py
scripts/bootstrap/git.py
scripts/bootstrap/docker.py
scripts/bootstrap/ansible_controller.py
```

Bootstrap предназначен только для первичного запуска и передачи управления штатному provisioning. Произвольные packages, Docker workloads и прикладные сервисы через deployer не устанавливаются.

Специальный `311-dev-services` использует bootstrap `base + git + docker + ansible_controller`; Ansible работает контейнеризированно как Execution Environment. После этого повторяемая настройка Linux и приложений выполняется Ansible.

Архитектурная policy: [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md).
