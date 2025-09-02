import os
import logging
import sqlite3
import time
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# -------- Logging --------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -------- ENV --------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Провайдер LLM: 'groq' или 'deepseek'
PROVIDER = os.getenv("PROVIDER", "deepseek").lower()

# DeepSeek
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Groq
GROQ_KEY = os.getenv("GROQ_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# Monetization/limits
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://t.me/CryptoBot")

# Memory
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
