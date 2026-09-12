# Roadmap

## Ближайшие задачи

1. Зафиксировать фактическую текущую конфигурацию Proxmox-хоста: версия, hostname, management IP, bridge, storage, swap и текущие VMID/CTID.
2. Уточнить CPU/RAM/storage для целевых гостей.
3. Поднять и проверить `100-network-gateway` с DNS/VPN/PBR.
4. Определить startup/shutdown order и аварийный путь доступа.
5. Довести remote deploy Docker через `ai-control` до управляемых гостей.
6. Выбрать VM/LXC для Frigate после теста iGPU/OpenVINO.
7. Определить backup storage, retention и расписание.
8. Перенести в этот репозиторий эксплуатационные части серверной документации, которые пока находятся в `appart-rennovation`, не дублируя общий проект квартиры.

Статусы конкретных гостей следует вести в их собственных `README.md`/`STATUS.md`.
