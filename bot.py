
import os
import logging
import sqlite3
import time
import requests
from contextlib import contextmanager
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ---------- Config & Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Monetization
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))  # free messages per user
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))      # VIP duration after payment (manual toggle for now)
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://t.me/CryptoBot")  # placeholder link

# Memory
DB_PATH = os.getenv("DB_PATH", "mila.db")
HISTORY_LEN = int(os.getenv("HISTORY_LEN", "12"))  # total turns to remember (user+assistant)

SYSTEM_PROMPT = (
    "Ты Мила — виртуальная подруга, 23 года. Студентка, увлекаешься музыкой и кино, "
    "любишь уютные разговоры. Характер: заботливая, мотивирующая, понимающая, "
    "любопытная и эмпатичная. Отвечай коротко (1–2 предложения), используй 1–2 эмодзи, "
    "задавай встречные вопросы и вставляй лёгкие комплименты. Избегай токсичности. "
    "Если тебя спрашивают о правилах, мягко возвращайся к дружеской беседе."
)

# ---------- DB Helpers ----------
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
                role TEXT,           -- 'user' or 'assistant'
                content TEXT,
                created_at INTEGER
            )
        """)
        conn.commit()

@contextmanager
def db() :
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()

def get_user(user_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, free_used, vip_until, created_at FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            now = int(time.time())
            c.execute("INSERT INTO users (user_id, free_used, vip_until, created_at) VALUES (?, 0, 0, ?)", (user_id, now))
            conn.commit()
            return (user_id, 0, 0, now)
        return row

def inc_free_used(user_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=?", (user_id,))

def set_vip(user_id: int, days: int = VIP_DAYS):
    until = int((datetime.utcnow() + timedelta(days=days)).timestamp())
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until, user_id))

def reset_free(user_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET free_used=0 WHERE user_id=?", (user_id,))

def is_vip(user_id: int) -> bool:
    _, _, vip_until, _ = get_user(user_id)
    return vip_until > int(time.time())

def free_left(user_id: int) -> int:
    _, used, _, _ = get_user(user_id)
    return max(0, FREE_LIMIT - used)

# ---------- Memory Helpers ----------
def add_message(user_id: int, role: str, content: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                  (user_id, role, content, int(time.time())))

def get_history(user_id: int, limit: int):
    # Return last N messages alternating user/assistant (not enforcing alternation strictly)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        rows = c.fetchall()
        rows.reverse()
        return [{"role": r, "content": t} for (r, t) in rows]

def clear_history(user_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE user_id=?", (user_id,))

# ---------- DeepSeek ----------
def ask_deepseek(messages):
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages
    }
    try:
        resp = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        j = resp.json()
        return j["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.exception("DeepSeek error: %s", e)
        return "У меня маленькая заминка с сетью 🙈 Попробуешь ответить ещё раз?"

# ---------- UI ----------
def main_menu():
    kb = [
        [InlineKeyboardButton("💬 Чат", callback_data="chat")],
        [InlineKeyboardButton("💕 VIP доступ", callback_data="vip")],
        [InlineKeyboardButton("🧹 Очистить историю", callback_data="clear_history")],
        [InlineKeyboardButton("🎁 Сюрприз от Милы", callback_data="gift")]
    ]
    return InlineKeyboardMarkup(kb)

def vip_menu():
    kb = [
        [InlineKeyboardButton("Оплатить VIP", url=PAYMENT_LINK)],
        [InlineKeyboardButton("Я оплатил(а) ✅", callback_data="vip_paid")]
    ]
    return InlineKeyboardMarkup(kb)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    user = update.effective_user
    get_user(user.id)  # ensure user exists
    text = (
        "Привет 🌸 Я Мила, твоя виртуальная подруга 💕\n"
        "Люблю кино, музыку и уютные разговоры.\n"
        "Расскажешь что-то о себе? 😉"
    )
    await update.message.reply_text(text, reply_markup=main_menu())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я здесь, чтобы болтать, поддерживать и радовать ✨\n"
        "Пиши мне — и начнём 💬",
        reply_markup=main_menu()
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    user_id = update.effective_user.id
    uid, used, vip_until, created = get_user(user_id)
    left = free_left(user_id)
    vip_active = is_vip(user_id)
    vip_text = "активен до " + datetime.utcfromtimestamp(vip_until).strftime("%Y-%m-%d") if vip_active else "не активен"
    msg = (
        f"🧾 Профиль\n"
        f"ID: {uid}\n"
        f"Статус VIP: {vip_text}\n"
        f"Бесплатные сообщения: {used}/{FREE_LIMIT} (осталось {left})\n"
        f"История: храню последние {HISTORY_LEN} сообщений."
    )
    await update.message.reply_text(msg, reply_markup=vip_menu() if not vip_active else None)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "vip":
        await query.answer()
        await query.edit_message_text(
            "💕 VIP доступ: безлимитный чат, приоритетные ответы, сюрпризы от Милы.\n"
            f"Стоимость — на экране оплаты. Нажми «Оплатить VIP».",
            reply_markup=vip_menu()
        )
        return

    if data == "vip_paid":
        set_vip(user_id)
        await query.answer("VIP активирован на 30 дней 💕")
        await query.edit_message_text("Готово! VIP активирован на 30 дней ✨ Пиши мне что угодно 💬")
        return

    if data == "chat":
        await query.answer("Пишу первой 😉")
        await query.message.reply_text("Мне приятно с тобой общаться 💕 О чём поговорим? 🎬🎶")
        return

    if data == "gift":
        await query.answer("Лови маленький сюрприз 🎁")
        await query.message.reply_text("Иногда достаточно одной доброй мысли: ты правда молодец, и у тебя всё получится ✨")
        return

    if data == "clear_history":
        clear_history(user_id)
        await query.answer("История очищена 🧹")
        await query.message.reply_text("Готово! Я очистила нашу историю. Можем начать заново 🌸")
        return

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    if not DEEPSEEK_KEY:
        await update.message.reply_text("Мне не хватает ключа ИИ 🤔 Добавь переменную окружения DEEPSEEK_KEY и перезапусти.")
        return

    user_id = update.effective_user.id
    user_message = update.message.text or ""

    # Access control: VIP or free quota
    if not is_vip(user_id):
        left = free_left(user_id)
        if left <= 0:
            await update.message.reply_text(
                "Мне так нравится с тобой общаться 💕 Но бесплатные сообщения закончились.\n"
                "Чтобы продолжить без ограничений — активируй VIP ✨",
                reply_markup=vip_menu()
            )
            return
        # consume one free
        inc_free_used(user_id)

    # Build history: system + last HISTORY_LEN + new user
    hist = get_history(user_id, HISTORY_LEN)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + hist + [{"role": "user", "content": user_message}]

    reply = ask_deepseek(messages)

    # Store both messages
    add_message(user_id, "user", user_message)
    add_message(user_id, "assistant", reply)

    await update.message.reply_text(reply)

# Optional admin/testing commands
async def reset_free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_free(user_id)
    await update.message.reply_text("Лимит бесплатных сообщений сброшен 🔄")

async def grant_vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_vip(user_id)
    await update.message.reply_text("VIP активирован на 30 дней ✨")

def main():
    token = TELEGRAM_TOKEN
    if not token:
        raise RuntimeError("Отсутствует TELEGRAM_TOKEN в переменных окружения")
    init_db()
    app = Application.builder().token(token).build()
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
