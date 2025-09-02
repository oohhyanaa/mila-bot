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

# Провайдер LLM: 'groq' (рекомендуется для бесплатного теста) или 'deepseek'
PROVIDER = os.getenv("PROVIDER", "deepseek").lower()

# DeepSeek
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Groq
GROQ_KEY = os.getenv("GROQ_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# Ограничения/монетизация
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://t.me/CryptoBot")

# Память
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

# ---------- LLM Providers ----------
def ask_deepseek(messages):
    if not DEEPSEEK_KEY:
        return "Нужен DEEPSEEK_KEY для DeepSeek 🙈"
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}"}
    payload = {"model": DEEPSEEK_MODEL, "messages": messages}
    r = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def ask_groq(messages):
    if not GROQ_KEY:
        return "Нужен GROQ_KEY для Groq 🙈"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    payload = {"model": GROQ_MODEL, "messages": messages}
    r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def ask_llm(messages):
    try:
        if PROVIDER == "groq":
            return ask_groq(messages)
        else:
            return ask_deepseek(messages)
    except requests.HTTPError as e:
        logger.exception("LLM HTTP error: %s", e)
        code = e.response.status_code if e.response is not None else None
        if code == 402:
            return "Похоже, на аккаунте закончились кредиты для ИИ 😅 Попробуешь ещё раз позже?"
        return "У меня небольшая сетевая заминка 🙈 Давай попробуем ещё раз?"
    except Exception as e:
        logger.exception("LLM error: %s", e)
        return "Немного замешкалась 🙈 Повтори, пожалуйста?"

# ---------- UI ----------
def main_menu():
    kb = [
        [InlineKeyboardButton("💬 Чат", callback_data="chat")],
        [InlineKeyboardButton("🧹 Очистить историю", callback_data="clear_history")],
        [InlineKeyboardButton("💕 VIP доступ", callback_data="vip")],
    ]
    return InlineKeyboardMarkup(kb)

def vip_menu():
    kb = [
        [InlineKeyboardButton("Оплатить VIP", url=PAYMENT_LINK)],
        [InlineKeyboardButton("Я оплатил(а) ✅", callback_data="vip_paid")],
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
        f"Провайдер ИИ: {PROVIDER}"
    )
    await update.message.reply_text(msg)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    data = q.data
    if data == "vip":
        await q.answer()
        await q.edit_message_text(
            "💕 VIP доступ: безлимитный чат, приоритетные ответы, сюрпризы от Милы.\n"
            "Нажми «Оплатить VIP», а затем «Я оплатил(а) ✅».",
            reply_markup=vip_menu()
        )
    elif data == "vip_paid":
        set_vip(user_id)
        await q.answer("VIP активирован на 30 дней 💕")
        await q.edit_message_text("Готово! VIP активирован ✨ Пиши мне что угодно 💬")
    elif data == "chat":
        await q.answer("Я здесь 😉")
        await q.message.reply_text("О чём поговорим? 🎬🎶")
    elif data == "clear_history":
        clear_history(user_id)
        await q.answer("История очищена 🧹")
        await q.message.reply_text("Начнём с чистого листа 🌸")

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

    # Память
    hist = get_history(user_id, HISTORY_LEN)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + hist + [{"role": "user", "content": user_message}]
    reply = ask_llm(messages)

    add_message(user_id, "user", user_message)
    add_message(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# Доп. команды для тестов
async def reset_free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_free(update.effective_user.id)
    await update.message.reply_text("Лимит бесплатных сообщений сброшен 🔄")

async def grant_vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_vip(update.effective_user.id)
    await update.message.reply_text("VIP активирован на 30 дней ✨")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Отсутствует TELEGRAM_TOKEN в переменных окружения")
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("reset_free", reset_free_cmd))
    app.add_handler(CommandHandler("grant_vip", grant_vip_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
