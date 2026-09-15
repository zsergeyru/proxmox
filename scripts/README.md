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

## Management access

Для deployable Debian VM/LXC контракт management access:

```text
SSH root:22
password authentication disabled
public key authentication only
```

`deploy-guest.py` обязан обеспечить initial SSH **до** запуска optional bootstrap handlers:

```text
VM  → Cloud-Init sshkeys для root
LXC → ssh-public-keys для root
→ start
→ verify root SSH
```

Initial access не является capability `base`.

## Bootstrap guests

После готовности root SSH `deploy-guest.py` получает ограниченный расширяемый bootstrap-интерфейс.

Начальный whitelist:

```text
base
git
docker
ansible_controller
```

Реализация должна быть вынесена в отдельные идемпотентные handlers:

```text
scripts/bootstrap/base.py
scripts/bootstrap/git.py
scripts/bootstrap/docker.py
scripts/bootstrap/ansible_controller.py
```

`base` работает поверх уже доступного guest и не создаёт management user/SSH-доступ.

Bootstrap предназначен только для первичной подготовки и handoff к provisioning. Произвольные packages, Docker workloads и прикладные сервисы через deployer не устанавливаются.

Специальный `311-dev-services` может использовать bootstrap `base + git + docker + ansible_controller`; Ansible работает контейнеризированно как Execution Environment. Если `base` не требуется, это не влияет на root SSH.

После этого повторяемая настройка Linux и приложений выполняется Ansible отдельной SSH identity.

Архитектурная policy: [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md).
