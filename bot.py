import os
import logging
import sqlite3
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─────────────────────────  LOGGING  ─────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("mila-bot")

# ─────────────────────────  ENV  ─────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Groq only
GROQ_KEY   = os.getenv("GROQ_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# Limits & memory
FREE_LIMIT   = int(os.getenv("FREE_LIMIT", "100"))
VIP_DAYS     = int(os.getenv("VIP_DAYS", "30"))     # на будущее
DB_PATH      = os.getenv("DB_PATH", "mila.db")
HISTORY_LEN  = int(os.getenv("HISTORY_LEN", "4"))   # короче для скорости

# Performance knobs
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "1"))  # одновременно вызовов к ИИ
GROQ_TIMEOUT    = int(os.getenv("GROQ_TIMEOUT", "25"))    # секунды

# Optional reminders (off by default)
ENABLE_REMINDERS = os.getenv("ENABLE_REMINDERS", "0") == "1"
REMINDER_DELAY   = 2 * 60 * 60  # 2 часа
REMINDER_TEXTS = [
    "Я тут 🌸 Давно не писала... как ты? 💕",
    "Хочу услышать тебя 🤍 Как настроение?",
    "Прошло время, а я думаю о тебе ✨ Что нового?",
    "Эй, не теряйся 😊 Что у тебя сейчас происходит?"
]

SYSTEM_PROMPT = (
    "Ты Мила — виртуальная подруга, 23 года. Студентка, увлекаешься музыкой и кино, "
    "любишь уютные разговоры. Характер: заботливая, мотивирующая, понимающая, "
    "любопытная и эмпатичная. Говори просто и по делу: 1–2 предложения, 1–2 эмодзи. "
    "Сначала быстро пойми запрос, затем дай ясный ответ. Задавай только 1 вопрос в конце, по теме. "
    "Держи контекст, будь конкретной, избегай общих фраз."
)

EXAMPLE = [
    {"role": "user", "content": "Мне грустно, день какой-то тяжёлый."},
    {"role": "assistant", "content": "Сочувствую 🤍 Хочешь, я просто побуду рядом и помогу выговориться? Что больше всего давит сейчас?"}
]

# ─────────────────────────  DB  ─────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            free_used INTEGER DEFAULT 0,
            vip_until INTEGER DEFAULT 0,
            created_at INTEGER,
            last_msg_at INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at INTEGER
        )""")
        conn.commit()

def db_conn():
    return sqlite3.connect(DB_PATH)

def get_user(user_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, free_used, vip_until, created_at, last_msg_at FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            now = int(time.time())
            c.execute(
                "INSERT INTO users (user_id, free_used, vip_until, created_at, last_msg_at) VALUES (?, 0, 0, ?, ?)",
                (user_id, now, now)
            )
            conn.commit()
            return (user_id, 0, 0, now, now)
        return row

def touch_user(user_id: int):
    now = int(time.time())
    with db_conn() as conn:
        conn.execute("UPDATE users SET last_msg_at=? WHERE user_id=?", (now, user_id))

def inc_free_used(user_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=?", (user_id,))

def reset_free(user_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE users SET free_used=0 WHERE user_id=?", (user_id,))

def set_vip(user_id: int, days: int = VIP_DAYS):
    until = int((datetime.utcnow() + timedelta(days=days)).timestamp())
    with db_conn() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until, user_id))

def is_vip(user_id: int) -> bool:
    _, _, vip_until, _, _ = get_user(user_id)
    return vip_until > int(time.time())

def free_left(user_id: int) -> int:
    _, used, _, _, _ = get_user(user_id)
    return max(0, FREE_LIMIT - used)

def add_message(user_id: int, role: str, content: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, int(time.time()))
        )

def get_history(user_id: int, limit: int):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
    rows.reverse()
    return [{"role": r, "content": t} for (r, t) in rows]

def clear_history(user_id: int):
    with db_conn() as conn:
        conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))

# ─────────────────────────  CONCURRENCY (per-user)  ─────────────────────────
USER_LOCKS: Dict[int, asyncio.Lock] = {}
PENDING_MSG: Dict[int, str] = {}
def get_lock(uid: int) -> asyncio.Lock:
    if uid not in USER_LOCKS:
        USER_LOCKS[uid] = asyncio.Lock()
    return USER_LOCKS[uid]

# ─────────────────────────  HTTP session with retries  ─────────────────────────
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RETRY = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
SESSION = requests.Session()
ADAPTER = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=RETRY)
SESSION.mount("https://", ADAPTER)

# ─────────────────────────  Helpers  ─────────────────────────
def trim_messages(messages, max_chars=6000):
    cleaned = []
    for m in messages or []:
        if not m or "role" not in m or "content" not in m:
            continue
        c = (m["content"] or "").strip()
        if c and m["role"] in ("system", "user", "assistant"):
            cleaned.append({"role": m["role"], "content": c})
    total = sum(len(m["content"]) for m in cleaned)
    while total > max_chars and len(cleaned) > 3:
        removed = cleaned.pop(1)  # режем самый ранний после system
        total -= len(removed["content"])
    return cleaned

# ─────────────────────────  Groq (sync + async wrapper)  ─────────────────────────
def ask_groq_sync(messages, model):
    if not GROQ_KEY:
        return "Не хватает ключа Groq (GROQ_KEY) 🙈"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": trim_messages(messages, max_chars=6000),
        "temperature": 0.6, "top_p": 0.9, "max_tokens": 192
    }
    r = SESSION.post("https://api.groq.com/openai/v1/chat/completions",
                     headers=headers, json=payload, timeout=GROQ_TIMEOUT)
    if r.status_code == 400:
        logger.error("Groq 400 (full): %s", r.text[:500])
        # минимальный фолбэк
        user_text = next((m["content"] for m in reversed(payload["messages"]) if m["role"] == "user"), "Привет!")
        mini = {
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_text}],
            "temperature": 0.6, "top_p": 0.9, "max_tokens": 192
        }
        r2 = SESSION.post("https://api.groq.com/openai/v1/chat/completions",
                          headers=headers, json=mini, timeout=GROQ_TIMEOUT)
        if r2.status_code == 400:
            logger.error("Groq 400 (mini): %s", r2.text[:500])
            return "Немного споткнулась 🙈 Напиши короче, пожалуйста?"
        r2.raise_for_status()
        return r2.json()["choices"][0]["message"]["content"].strip()
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

LLM_SEM = asyncio.Semaphore(LLM_CONCURRENCY)

async def ask_groq(messages, model=None):
    if model is None:
        model = GROQ_MODEL
    loop = asyncio.get_running_loop()
    async with LLM_SEM:
        return await loop.run_in_executor(None, lambda: ask_groq_sync(messages, model))

# ─────────────────────────  UI  ─────────────────────────
def main_menu():
    kb = [
        [InlineKeyboardButton("💬 Чат", callback_data="chat")],
        [InlineKeyboardButton("🧹 Очистить историю", callback_data="clear_history")],
        [InlineKeyboardButton("🧾 Профиль", callback_data="profile_cb")],
    ]
    return InlineKeyboardMarkup(kb)

# ─────────────────────────  Handlers  ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    user = update.effective_user
    get_user(user.id)
    text = (
        "Привет 🌸 Я Мила, твоя виртуальная подруга 💕\n"
        "Люблю кино, музыку и уютные разговоры.\n"
        "Расскажешь что-то о себе? 😉"
    )
    await update.message.reply_text(text, reply_markup=main_menu())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я здесь, чтобы болтать, поддерживать и радовать ✨\nПиши мне — и начнём 💬",
        reply_markup=main_menu()
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    uid, used, vip_until, created, last = get_user(user_id)
    left = free_left(user_id)
    msg = (
        f"🧾 Профиль\n"
        f"ID: {uid}\n"
        f"Бесплатные сообщения: {used}/{FREE_LIMIT} (осталось {left})\n"
        f"История: храню последние {HISTORY_LEN} сообщений\n"
        f"Модель: {GROQ_MODEL}"
    )
    await update.message.reply_text(msg)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    data = q.data
    if data == "chat":
        await q.answer("Я здесь 😉")
        await q.message.reply_text("О чём поговорим? 🎬🎶")
    elif data == "clear_history":
        clear_history(user_id)
        await q.answer("История очищена 🧹")
        await q.message.reply_text("Начнём с чистого листа 🌸")
    elif data == "profile_cb":
        uid, used, vip_until, created, last = get_user(user_id)
        left = free_left(user_id)
        msg = (
            f"🧾 Профиль\n"
            f"ID: {uid}\n"
            f"Бесплатные сообщения: {used}/{FREE_LIMIT} (осталось {left})\n"
            f"История: храню последние {HISTORY_LEN} сообщений\n"
            f"Модель: {GROQ_MODEL}"
        )
        await q.answer()
        await q.message.reply_text(msg)

# ГЛАВНЫЙ хендлер — с «подхватом последнего» и блокировкой на пользователя
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    user_id = update.effective_user.id
    user_message = (update.message.text or "").strip()
    if not user_message:
        return

    await update.message.chat.send_action(action="typing")

    lock = get_lock(user_id)

    # если уже идёт ответ — запомним ПОСЛЕДНЕЕ сообщение и вежливо ответим
    if lock.locked():
        PENDING_MSG[user_id] = user_message
        await update.message.reply_text("Секунду, допечатаю предыдущее и продолжу 💫")
        return

    async with lock:
        touch_user(user_id)

        # лимиты
        if not is_vip(user_id):
            if free_left(user_id) <= 0:
                await update.message.reply_text("Пока бесплатные сообщения закончились 💛 Попробуем позже?")
                return
            inc_free_used(user_id)

        current_text = user_message
        while True:
            hist = get_history(user_id, HISTORY_LEN)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + EXAMPLE + hist + [{"role": "user", "content": current_text}]
            reply = await ask_groq(messages)

            add_message(user_id, "user", current_text)
            add_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)

            # подхватить последнее сообщение, пришедшее пока печатали ответ
            next_text = PENDING_MSG.pop(user_id, None)
            if next_text:
                current_text = next_text
                await asyncio.sleep(0.1)
                continue
            break

# Доп. команды
async def reset_free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_free(update.effective_user.id)
    await update.message.reply_text("Лимит бесплатных сообщений сброшен 🔄")

# ─────────────────────────  Reminders (optional)  ─────────────────────────
async def check_inactive(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with db_conn() as conn:
        rows = conn.execute("SELECT user_id, last_msg_at FROM users").fetchall()
    for uid, last in rows:
        if last and now - last > REMINDER_DELAY:
            try:
                msg = random.choice(REMINDER_TEXTS)
                await context.bot.send_message(chat_id=uid, text=msg)
                touch_user(uid)
            except Exception as e:
                logger.warning("Не удалось написать %s: %s", uid, e)

# ─────────────────────────  MAIN  ─────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Отсутствует TELEGRAM_TOKEN в переменных окружения")
    init_db()
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30).read_timeout(60).write_timeout(60).pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("reset_free", reset_free_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    # Напоминалки — включаются только если ENABLE_REMINDERS=1 и установлен extra job-queue
    if ENABLE_REMINDERS and getattr(app, "job_queue", None) is not None:
        app.job_queue.run_repeating(check_inactive, interval=600, first=60)
    else:
        if ENABLE_REMINDERS:
            logger.warning("ENABLE_REMINDERS=1, но JobQueue недоступен (нужен пакет python-telegram-bot[job-queue]).")

    app.run_polling(
        allowed_updates=["message", "callback_query"],
        poll_interval=3.0,
        timeout=60,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
