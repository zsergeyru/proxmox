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
