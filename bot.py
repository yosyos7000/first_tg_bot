import asyncio
import os
import re
import asyncpg
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import anthropic

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 416065237
FREE_LIMIT = 5

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db = None

from aiogram.types import ReplyKeyboardRemove

async def init_db():
    global db
    db = await asyncpg.connect(DATABASE_URL)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            free_used INTEGER DEFAULT 0,
            is_paid BOOLEAN DEFAULT FALSE,
            total_requests INTEGER DEFAULT 0,
            subscription_until TIMESTAMP
        )
    """)
    await db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0")
    await db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_until TIMESTAMP")

async def get_user(user_id, username):
    row = await db.fetchrow("SELECT free_used, is_paid, subscription_until FROM users WHERE user_id = $1", user_id)
    if not row:
        await db.execute("INSERT INTO users (user_id, username) VALUES ($1, $2)", user_id, username)
        return 0, False, None
    # Проверяем не истекла ли подписка
    if row["is_paid"] and row["subscription_until"] and row["subscription_until"] < datetime.utcnow():
        await db.execute("UPDATE users SET is_paid = FALSE WHERE user_id = $1", user_id)
        await bot.send_message(user_id, 
            "⚠️ Ваша подписка истекла. Напишите /subscribe чтобы продлить.")
        return row["free_used"], False, row["subscription_until"]
    return row["free_used"], row["is_paid"], row["subscription_until"]

async def increment_usage(user_id):
    await db.execute("""
        UPDATE users SET free_used = free_used + 1, total_requests = total_requests + 1 
        WHERE user_id = $1
    """, user_id)

@dp.message(F.text == "/start")
async def start(message: Message):
    await message.answer(
        "Привет! Я ИИ-помощник.\n"
        f"У тебя {FREE_LIMIT} бесплатных запросов.\n"
        "Просто напиши свой вопрос!",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(F.text.in_({"👤 Мой профиль", "/profile"}))
async def profile(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    free_used, is_paid, sub_until = await get_user(uid, username)

    if is_paid and sub_until:
        sub_text = f"✅ Активна до: {sub_until.strftime('%d.%m.%Y %H:%M')}"
    elif sub_until and not is_paid:
        sub_text = f"❌ Истекла: {sub_until.strftime('%d.%m.%Y %H:%M')}"
    else:
        sub_text = f"🆓 Бесплатный план ({FREE_LIMIT - min(free_used, FREE_LIMIT)} запросов осталось)"

    await message.answer(
        f"👤 Профиль\n\n"
        f"🤖 Модель: Claude Sonnet\n"
        f"📋 Подписка: {sub_text}\n"
        f"📨 Всего запросов: {free_used}"
    )

@dp.message(F.text == "/subscribe")
async def subscribe(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    await message.answer("Заявка отправлена! Администратор свяжется с тобой.")
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить (+1 месяц)", callback_data=f"approve_{uid}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{uid}")
    await bot.send_message(
        ADMIN_ID,
        f"💰 Новая заявка на подписку!\n"
        f"👤 @{username}\n"
        f"🆔 ID: {uid}",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("approve_"))
async def approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split("_")[1])
    row = await db.fetchrow("SELECT subscription_until, is_paid FROM users WHERE user_id = $1", uid)
    
    # Если подписка ещё активна — продлеваем от текущей даты окончания
    if row and row["is_paid"] and row["subscription_until"] and row["subscription_until"] > datetime.utcnow():
        new_until = row["subscription_until"] + timedelta(days=30)
    else:
        new_until = datetime.utcnow() + timedelta(days=30)

    await db.execute(
        "UPDATE users SET is_paid = TRUE, subscription_until = $1 WHERE user_id = $2",
        new_until, uid
    )
    until_str = new_until.strftime('%d.%m.%Y %H:%M')
    await bot.send_message(uid, 
        f"✅ Подписка активирована!\n"
        f"📅 Действует до: {until_str}\n\n"
        f"Пользуйтесь без ограничений!")
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Одобрено до {until_str}"
    )
    await callback.answer("Подписка активирована!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split("_")[1])
    await bot.send_message(uid, 
        "❌ Заявка отклонена. Напишите администратору @polyakovkonst для уточнения.")
    await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
    await callback.answer("Заявка отклонена")

@dp.message(F.text == "/stats")
async def stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_users = await db.fetchval("SELECT COUNT(*) FROM users")
    paid_users = await db.fetchval("SELECT COUNT(*) FROM users WHERE is_paid = TRUE")
    total_requests = await db.fetchval("SELECT SUM(total_requests) FROM users")
    top_users = await db.fetch("""
        SELECT username, total_requests FROM users 
        ORDER BY total_requests DESC LIMIT 5
    """)
    top_text = "\n".join([f"@{r['username']} — {r['total_requests']} запросов" for r in top_users])
    await message.answer(
        f"📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💰 Платных: {paid_users}\n"
        f"📨 Всего запросов: {total_requests or 0}\n\n"
        f"🏆 Топ-5 пользователей:\n{top_text}"
    )

@dp.message()
async def handle(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    free_used, is_paid, sub_until = await get_user(uid, username)

    if uid != ADMIN_ID and not is_paid and free_used >= FREE_LIMIT:
        await message.answer(
            "Бесплатные запросы закончились.\n"
            "Напиши /subscribe чтобы оформить подписку — 299 руб./мес."
        )
        return

    await increment_usage(uid)
    await message.answer("Думаю...")

    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": message.text}]
    )

    text = response.content[0].text
    text = re.sub(r'\*\*?(.*?)\*\*?', r'\1', text)
    text = re.sub(r'#{1,6}\s?', '', text)
    await message.answer(text, reply_markup=ReplyKeyboardRemove())

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
