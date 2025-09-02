# Мила — виртуальная подруга (PRO + память)
Telegram-бот с лимитом бесплатных сообщений, VIP-экраном, и памятью диалога (SQLite).

## Ключевые фичи
- Память последних N сообщений на пользователя (ENV `HISTORY_LEN`, по умолчанию 12).
- Кнопка «Очистить историю».
- Профиль `/profile` показывает лимиты и статус VIP.
- Простая монетизация: кнопка оплаты и ручная активация VIP.

## Быстрый старт
1) Python 3.11+
2) `pip install -r requirements.txt`
3) Переменные окружения:
```
export TELEGRAM_TOKEN=...          
export DEEPSEEK_KEY=...            
export DEEPSEEK_MODEL=deepseek-chat
export FREE_LIMIT=10
export VIP_DAYS=30
export PAYMENT_LINK=https://t.me/CryptoBot
export DB_PATH=mila.db
export HISTORY_LEN=12
```
4) `python bot.py`

## Railway/Render
- Подключи репозиторий, задай переменные окружения, деплой.
- Файл БД `mila.db` будет создан автоматически в рабочей директории контейнера.

## Очистка/отладка
- `/reset_free` — сбросить лимит бесплатных сообщений (удобно для теста).
- Кнопка «🧹 Очистить историю» в главном меню.
