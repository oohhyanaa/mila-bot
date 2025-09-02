import os
import logging
import sqlite3
import time
import requests
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# -------- Logging --------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------- ENV --------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Только Groq
GROQ_KEY = os.getenv("GROQ_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# Ограничения/монетизация (VIP пока не используем, но оставлю задел)
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))  # на будущее
DB_PATH = os.getenv("DB_PATH", "mila.db")
HISTORY_LEN = int(os.getenv("HISTORY_LEN", "12"))

SYSTEM_PROMPT = (
    "Ты Мила — виртуальная подруга, 23 года. Студентка, увлекаешься музыкой и кино, "
    "любишь уютные разговоры. Характер: заботливая, мотивирующая, понимающая, "
    "любопытная и эмпатичная. Отвечай коротко (1–2 предложения), используй 1–2 эмодзи, "
    "задавай встречные вопросы и вставляй лёгкие комплименты. Избегай токсичности. "
    "Если тебя спрашивают о правилах, мягко возвращайся к дружеской беседе."
)

# ---------- DB ----------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                free_used INTEGER DEFAULT 0,
                vip_until INTEGER DEFAULT 0,
                created_at INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                created_at INTEGER
            )
        """)
        conn.commit()

def db_conn():
    return sqlite3.connect(DB_PATH)

def get_user(user_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, free_used, vip_until, created_at FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            now = int(time.time())
            c.execute(
                "INSERT INTO users (user_id, free_used, vip_until, created_at) VALUES (?, 0, 0, ?)",
                (user_id, now)
            )
            conn.commit()
            return (user_id, 0, 0, now)
        return row

def inc_free_used(user_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=?", (user_id,))

def set_vip(user_id: int, days: int = VIP_DAYS):
    until = int((datetime.utcnow() + timedelta(days=days)).timestamp())
    with db_conn() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until, user_id))

def reset_free(user_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE users SET free_used=0 WHERE user_id=?", (user_id,))

def is_vip(user_id: int) -> bool:
    _, _, vip_until, _ = get_user(user_id)
    return vip_until > int(time.time())

def free_left(user_id: int) -> int:
    _, used, _, _ = get_user(user_id)
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

# ---------- LLM (Groq) ----------
def ask_groq(messages):
    if not GROQ_KEY:
        return "Не хватает ключа Groq (GROQ_KEY) 🙈 Добавь его в переменные окружения на хостинге."
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    payload = {"model": GROQ_MODEL, "messages": messages}
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=payload, timeout=60
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except requests.HTTPError as e:
        logger.exception("Groq HTTP error: %s", e)
        code = e.response.status_code if e.response is not None else None
        if code == 401 or code == 403:
            return "Ключ Groq отклонён (401/403). Проверь правильность GROQ_KEY."
        return "Немного замешкалась из-за сети 🙈 Давай попробуем ещё раз?"
    except Exception as e:
        logger.exception("Groq error: %s", e)
        return "У меня небольшая заминка 🙈 Повтори, пожалуйста?"

# ---------- UI ----------
def main_menu():
    kb = [
        [InlineKeyboardButton("💬 Чат", callback_data="chat")],
        [InlineKeyboardButton("🧹 Очистить историю", callback_data="clear_history")],
        [InlineKeyboardButton("🧾 Профиль", callback_data="profile_cb")],
    ]
    return InlineKeyboardMarkup(kb)

# ---------- Handlers ----------
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
    uid, used, vip_until, created = get_user(user_id)
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
        # Показать профиль по кнопке
        uid, used, vip_until, created = get_user(user_id)
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

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    user_id = update.effective_user.id
    user_message = update.message.text or ""

    # Лимит бесплатных сообщений
    if not is_vip(user_id):
        if free_left(user_id) <= 0:
            await update.message.reply_text("Пока бесплатные сообщения закончились 💛 Попробуем позже?")
            return
        inc_free_used(user_id)

    # Память + системный промпт
    hist = get_history(user_id, HISTORY_LEN)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + hist + [{"role": "user", "content": user_message}]
    reply = ask_groq(messages)

    add_message(user_id, "user", user_message)
    add_message(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# Доп. команды
async def reset_free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_free(update.effective_user.id)
    await update.message.reply_text("Лимит бесплатных сообщений сброшен 🔄")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Отсутствует TELEGRAM_TOKEN в переменных окружения")
    init_db()
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        # Более устойчивые таймауты для Render
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("reset_free", reset_free_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=2.0,
        timeout=30,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
