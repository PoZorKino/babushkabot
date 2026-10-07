# Бабуль, не подскажешь?

Telegram-бот: на любое сообщение отвечает как русская бабушка. Ответ печатается
по мере генерации (стриминг через правку сообщения), Markdown рендерится.
Модель - бесплатная, через любой OpenAI-совместимый API (по умолчанию OpenRouter `:free`).

## Запуск

```
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # вписать TELEGRAM_BOT_TOKEN и LLM_API_KEY
python -m babushka
```

- Токен бота: @BotFather. Ключ OpenRouter (бесплатный): https://openrouter.ai/keys
- `/reset` - забыть историю чата. История хранится в памяти (последние `HISTORY_LIMIT` сообщений).
- Список моделей в `LLM_MODELS` - фолбэк по порядку, если первая упала или упёрлась в лимит.
