"""app/ — headless-оркестрация ads-слоя (Контур A / MCP), без импорта bot/aiogram.

`bot/main.py` поднимает тот же ads-слой для Telegram-бота; `app/` делает это для
MCP-сервера и скриптов. Единственный общий инвариант: модульные глобалы `ads/client.py`
должен засеять bootstrap — см. `app.bootstrap`.
"""
