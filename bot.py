import asyncio
import os
import re
import psycopg2
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
import anthropic

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 416065237
FREE_LIMIT = 5

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            free_used INTEGER DEFAULT 0,
            is_paid BOOLEAN DEFAULT FALSE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id, username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT free_used, is_paid FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s)", (user_id, username))
        conn.commit()
        row = (0, False)
    cur.close()
    conn.close()
    return row

def increment_usage(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()

@dp.message(F.text == "/start")
async def start(message: Message):
    await message.answer(
        "Привет! Я ИИ-помощник.\n"
        f"У тебя {FREE_LIMIT} бесплатных запросов.\n"
        "Просто напиши свой вопрос!"
    )

@dp.message(F.text == "/subscribe")
async def subscribe(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    await message.answer("Заявка отправлена! Администратор свяжется с тобой.")
    await bot.send_message(
        ADMIN_ID,
        f"💰 Новая заявка на подписку!\n"
        f"👤 @{username}\n"
        f"🆔 ID: {uid}"
    )

@dp.message()
async def handle(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    free_used, is_paid = get_user(uid, username)

    if uid != ADMIN_ID and not is_paid and free_used >= FREE_LIMIT:
        await message.answer(
            "Бесплатные запросы закончились.\n"
            "Напиши /subscribe чтобы оформить подписку — 299 руб./мес."
        )
        return

    increment_usage(uid)
    await message.answer("Думаю...")

    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": message.text}]
    )

    text = response.content[0].text
    text = re.sub(r'\*\*?(.*?)\*\*?', r'\1', text)
    text = re.sub(r'#{1,6}\s?', '', text)
    await message.answer(text)

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
