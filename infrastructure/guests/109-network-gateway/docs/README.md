# Документация 109 — network-gateway

Локальная документация VM 109-network-gateway.

Она описывает целевое состояние конкретного гостя: как он должен быть устроен, какие службы на нём работают, как проходит трафик, как его проверить и как вернуть сеть в рабочее состояние.

## Документы

| Файл | Назначение |
|---|---|
| [network-gateway.md](network-gateway.md) | Полная спецификация VM 109: сеть, службы, запуск, проверка и восстановление |
| [smartdns.md](smartdns.md) | Локальная спецификация SmartDNS на 109 |
| [routing-vpn.md](routing-vpn.md) | nftables, PBR, обычный WAN и VPN-маршруты |
| [implementation-status.md](implementation-status.md) | Что уже реализовано и что ещё требуется сделать |

Причины ключевых решений хранятся отдельно в каталоге [../decisions/](../decisions/).

## Общая документация проекта

- [Обзор сети](../../../../docs/400-network/400-overview.md)
- [Схема сети](../../../../docs/400-network/410-network-layout.md)
- [DNS](../../../../docs/400-network/420-dns.md)
- [Маршрутизация](../../../../docs/400-network/430-routing.md)
- [VPN](../../../../docs/400-network/440-vpn.md)
