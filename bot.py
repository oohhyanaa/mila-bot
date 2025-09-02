import os
import logging
import sqlite3
import time
import random
import requests
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, JobQueue
)

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Groq only
GROQ_KEY = os.getenv("GROQ_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")  # безопасная дефолтная модель

# Limits & memory
FREE_LIMIT   = int(os.getenv("FREE_LIMIT", "100"))
VIP_DAYS     = int(os.getenv("VIP_DAYS", "30"))  # на будущее
DB_PATH      = os.getenv("DB_PATH", "mila.db")
HISTORY_LEN  = int(os.getenv("HISTORY_LEN", "20"))

# 2h reminders
REMINDER_DELAY = 2 * 60 * 60       # 2 часа в секундах
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

# ---------- DB ----------
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

def set_vip(user_id: int, days: int = VIP_DAYS):
    until = int((datetime.utcnow() + timedelta(days=days)).timestamp())
    with db_conn() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until, user_id))

def reset_free(user_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE users SET free_used=0 WHERE user_id=?", (user_id,))

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

# ---------- Groq (LLM) ----------
def ask_groq(messages):
    if not GROQ_KEY:
        return "Не хватает ключа Groq (GROQ_KEY) 🙈"

    # Очистка входа от мусора/None/пустых
    clean = []
    for m in messages or []:
        if not m:
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("system", "user", "assistant") and isinstance(content, str) and content.strip():
            clean.append({"role": role, "content": content.strip()})
    if not any(m["role"] == "user" for m in clean):
        clean.append({"role": "user", "content": "Привет!"})

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }

    # fallback-лист моделей
    candidates = [
        os.getenv("GROQ_MODEL", GROQ_MODEL),
        "llama-3.1-8b-instant",
        "llama-3.1-70b-versatile"
    ]

    last_err_text = ""
    for model in candidates:
        payload = {
            "model": model,
            "messages": clean,
            "temperature": 0.6,
            "top_p": 0.9,
            "max_tokens": 320
        }
        for attempt in range(2):
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers, json=payload, timeout=45
                )
                if r.status_code == 400:
                    last_err_text = r.text  # покажем, что именно не понравилось API
                    break  # пробуем следующую модель
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except requests.RequestException as e:
                last_err_text = getattr(e.response, "text", str(e))
                time.sleep(1.0 * (attempt + 1))
        # следующая модель, если 400/ошибка

    logger.error("Groq 400/Request error. Details: %s", last_err_text)
    return "Немного замешкалась 🙈 Кажется, модель занята или запрос ей не понравился. Попробуем ещё раз?"

# ---------- Reminders ----------
async def check_inactive(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with db_conn() as conn:
        rows = conn.execute("SELECT user_id, last_msg_at FROM users").fetchall()
    for uid, last in rows:
        if last and now - last > REMINDER_DELAY:
            try:
                msg = random.choice(REMINDER_TEXTS)
                await context.bot.send_message(chat_id=uid, text=msg)
                touch_user(uid)  # чтобы не спамила каждую проверку
            except Exception as e:
                logger.warning("Не удалось написать %s: %s", uid, e)

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
    uid, used, vip_until, created, last = get_user(user_id)
    left = free_left(user_id)
    msg = (
        f"🧾 Профиль\n"
        f"ID: {uid}\n"
        f"Бесплатные сообщения: {used}/{FREE_LIMIT} (осталось {left})\n"
        f"История: храню последние {HISTORY_LEN} сообщений\n"
        f"Модель: {os.getenv('GROQ_MODEL', GROQ_MODEL)}"
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
            f"Модель: {os.getenv('GROQ_MODEL', GROQ_MODEL)}"
        )
        await q.answer()
        await q.message.reply_text(msg)

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    user_id = update.effective_user.id
    user_message = update.message.text or ""

    touch_user(user_id)  # обновим last_msg_at

    if not is_vip(user_id):
        if free_left(user_id) <= 0:
            await update.message.reply_text("Пока бесплатные сообщения закончились 💛 Попробуем позже?")
            return
        inc_free_used(user_id)

    hist = get_history(user_id, HISTORY_LEN)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + EXAMPLE + hist + [{"role": "user", "content": user_message}]
    reply = ask_groq(messages)

    add_message(user_id, "user", user_message)
    add_message(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# ---------- Extra cmds ----------
async def reset_free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_free(update.effective_user.id)
    await update.message.reply_text("Лимит бесплатных сообщений сброшен 🔄")

# ---------- Main ----------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Отсутствует TELEGRAM_TOKEN в переменных окружения")
    init_db()
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
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

    # Периодическая проверка «молчаливых» пользователей
    job_queue: JobQueue = app.job_queue
    job_queue.run_repeating(check_inactive, interval=600, first=60)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=2.0,
        timeout=30,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
