# 201 — ha-main

**Тип:** LXC  
**Назначение:** основной Home Assistant новой квартиры.

Этот гость не должен содержать общесерверные сервисы вроде DNS, Git, monitoring или Whisper/TTS. Инфраструктура MQTT/Zigbee2MQTT/ESPHome вынесена в `211-automation-services`.

Ресурсы и способ установки Home Assistant уточняются после развёртывания. Фактические файлы гостевой ОС должны храниться под `rootfs/`.
