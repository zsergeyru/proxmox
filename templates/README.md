# Шаблоны

Старый PVE-side контур сборки Debian 13 удалён.

Все новые шаблоны Proxmox описываются только через Packer:

```text
packer/<VMID>/
```

Для базового Debian 13:

```text
packer/9000/
```

VMID `9000` создаёт и проверяет `910 infra-manager`.
