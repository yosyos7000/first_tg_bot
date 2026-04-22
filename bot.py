import asyncio
import os
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
import anthropic

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")

ADMIN_ID = 416065237
PAID_USERS = set()
FREE_LIMIT = 5
free_limits = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

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
    used = free_limits.get(uid, 0)

    if uid != ADMIN_ID and uid not in PAID_USERS and used >= FREE_LIMIT:
        await message.answer(
            "Бесплатные запросы закончились.\n"
            "Напиши /subscribe чтобы оформить подписку — 299 руб./мес."
        )
        return

    free_limits[uid] = used + 1
    await message.answer("Думаю...")

    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": message.text}]
    )

    text = response.content[0].text
    text = re.sub(r'\*\*?(.*?)\*\*?', r'\1', text)
    text
