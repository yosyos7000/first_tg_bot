import asyncio
import os
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
import anthropic

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")

free_limits = {}
FREE_LIMIT = 5

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

@dp.message()
async def handle(message: Message):
    uid = message.from_user.id
    used = free_limits.get(uid, 0)

    if uid not in set() and used >= FREE_LIMIT:
        await message.answer(
            "Бесплатные запросы закончились.\n"
            "Подписка: 299 руб./мес.\n"
            "Написать администратору: @polyakovkonst"
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
    text = re.sub(r'#{1,6}\s?', '', text)
    await message.answer(text)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
