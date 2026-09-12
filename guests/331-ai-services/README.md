# 331 — ai-services

**Тип:** LXC  
**Назначение:** общие AI-сервисы, не зависящие от конкретного агента.

Планируемый Docker Compose:

- Whisper / faster-whisper — общий STT API;
- TTS — Kokoro, Piper или другой выбранный движок.

Этими API могут пользоваться Hermes, Home Assistant, голосовые панели, Open WebUI и будущие приложения. Перезапуск `301-ai-control` или текущего bootstrap `320-ai-control` не должен останавливать общие голосовые функции.

Compose и конфиги после появления реальной конфигурации хранятся под `rootfs/`.
