# Packer

Этот каталог — основной источник сборки шаблонов Proxmox через Packer.

Каждый постоянный шаблон имеет собственный каталог по VMID:

```text
automation/packer/
├── 9000/
├── 9100/
└── ...
```

Каталог шаблона является самостоятельной единицей сборки. Имя операционной системы не используется как имя каталога, потому что VMID однозначно связывает конфигурацию Packer с объектом Proxmox.

Минимальная структура:

```text
automation/packer/<VMID>/
├── template.pkr.hcl
├── variables.pkr.hcl
├── http/
│   └── preseed.cfg
└── scripts/
    ├── setup.sh
    └── cleanup.sh
```

`template.pkr.hcl` описывает сам шаблон и последовательность сборки.

`variables.pkr.hcl` описывает только входные параметры среды: API Proxmox, node, storage, bridge, установочный ISO и временный пароль сборки.

`http/preseed.cfg` автоматизирует установку Debian. Packer временно отдаёт его установщику по HTTP непосредственно из Runner 910; файл не является частью готового шаблона.

`scripts/setup.sh` настраивает установленную ОС. `scripts/cleanup.sh` удаляет уникальные данные сборщика и готовит систему к клонированию.

Packer отвечает за жизненный цикл VM-сборщика и превращение её в template. OpenTofu не строит шаблоны.

Текущий базовый шаблон `9000` имеет `Template-Version 8`. Сборка запускается через `scripts/infra-manager/jobs/build-template.sh 9000`. После успешной реальной сборки и Full Clone проверки старый PVE-side контур Template-Version 7 удалён; Packer является единственным источником сборки VM template 9000.
