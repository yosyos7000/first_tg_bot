import asyncio
import os
import re
import asyncpg
import aiohttp
import base64
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import ReplyKeyboardRemove
import anthropic

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 416065237
FREE_LIMIT = 5
MAX_HISTORY = 10

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db = None
conversations = {}
user_roles = {}

ROLES = {
    "business": {
        "name": "🧑‍💼 Бизнес-ассистент",
        "prompt": "Ты профессиональный бизнес-ассистент. Помогаешь с анализом данных, составлением документов, деловой перепиской, стратегическими решениями и бизнес-задачами. Отвечай чётко, структурированно и профессионально."
    },
    "copywriter": {
        "name": "✍️ Копирайтер",
        "prompt": "Ты опытный копирайтер. Помогаешь писать тексты для соцсетей, рекламу, статьи, посты, описания товаров и услуг. Пиши живо, убедительно и цепляюще."
    },
    "chat": {
        "name": "💬 Обычный чат",
        "prompt": "Ты дружелюбный и умный собеседник. Отвечай естественно, помогай с любыми вопросами, будь полезным и приятным в общении."
    }
}

WELCOME_TEXT = """👋 Привет! Я твой ИИ-помощник на базе Claude.

🤖 Что я умею:
- 💬 Отвечать на любые вопросы
- 🖼 Анализировать фотографии
- 📄 Читать файлы — PDF, Word, Excel, TXT
- ✍️ Писать тексты, письма, посты
- 📊 Анализировать данные и документы
- 🧑‍💼 Помогать с бизнес-задачами

📋 Команды:
/profile — твой профиль и статус подписки
/role — выбрать режим работы бота
/clear — очистить историю диалога
/help — как пользоваться ботом
/subscribe — оформить подписку

У тебя есть {limit} бесплатных запросов для знакомства.
Просто напиши свой вопрос или отправь файл! 👇"""

HELP_TEXT = """📖 Как пользоваться ботом

✏️ Текстовые запросы
Просто напиши свой вопрос — бот ответит. Можно задавать уточняющие вопросы, бот помнит контекст последних {history} сообщений.

📎 Файлы и фото
Отправь файл или фото с подписью или без. Бот умеет читать:
— Фотографии (JPG, PNG)
— Документы PDF
— Word файлы (.docx)
— Excel таблицы (.xlsx)
— Текстовые файлы (.txt)

🎭 Режимы работы (/role)
Выбери роль под свою задачу:
— 🧑‍💼 Бизнес-ассистент — деловые задачи, анализ, документы
— ✍️ Копирайтер — тексты, посты, реклама
— 💬 Обычный чат — общение и помощь по любым вопросам

🗑 Очистка контекста (/clear)
Бот помнит последние {history} сообщений диалога. Если хочешь начать новую тему с чистого листа — используй /clear. Это полезно когда предыдущий контекст мешает новому разговору.

💰 Подписка (/subscribe)
Бесплатно доступно {limit} запросов. Для безлимитного доступа оформи подписку — 299 руб./мес."""

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
    if row["is_paid"] and row["subscription_until"] and row["subscription_until"] < datetime.utcnow():
        await db.execute("UPDATE users SET is_paid = FALSE WHERE user_id = $1", user_id)
        await bot.send_message(user_id, "⚠️ Ваша подписка истекла. Напишите /subscribe чтобы продлить.")
        return row["free_used"], False, row["subscription_until"]
    return row["free_used"], row["is_paid"], row["subscription_until"]

async def increment_usage(user_id):
    await db.execute("""
        UPDATE users SET free_used = free_used + 1, total_requests = total_requests + 1
        WHERE user_id = $1
    """, user_id)

async def download_file(file_id):
    file = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()

def get_system_prompt(user_id):
    role_key = user_roles.get(user_id, "chat")
    return ROLES[role_key]["prompt"]

@dp.message(F.text == "/start")
async def start(message: Message):
    conversations.pop(message.from_user.id, None)
    await message.answer(
        WELCOME_TEXT.format(limit=FREE_LIMIT),
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(F.text == "/help")
async def help_cmd(message: Message):
    await message.answer(
        HELP_TEXT.format(history=MAX_HISTORY, limit=FREE_LIMIT)
    )

@dp.message(F.text == "/role")
async def role_cmd(message: Message):
    uid = message.from_user.id
    current = user_roles.get(uid, "chat")
    builder = InlineKeyboardBuilder()
    for key, role in ROLES.items():
        label = f"✅ {role['name']}" if key == current else role['name']
        builder.button(text=label, callback_data=f"role_{key}")
    builder.adjust(1)
    await message.answer(
        "🎭 Выбери режим работы бота:\n\n"
        "🧑‍💼 Бизнес-ассистент — деловые задачи, анализ, документы\n"
        "✍️ Копирайтер — тексты, посты, реклама\n"
        "💬 Обычный чат — общение и помощь по любым вопросам",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("role_"))
async def set_role(callback: CallbackQuery):
    uid = callback.from_user.id
    role_key = callback.data.split("_", 1)[1]
    if role_key not in ROLES:
        return
    user_roles[uid] = role_key
    conversations.pop(uid, None)
    role_name = ROLES[role_key]["name"]
    await callback.message.edit_text(
        f"✅ Режим изменён на: {role_name}\n\nИстория диалога очищена. Можешь начинать!"
    )
    await callback.answer()

@dp.message(F.text.in_({"👤 Мой профиль", "/profile"}))
async def profile(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    free_used, is_paid, sub_until = await get_user(uid, username)
    role_key = user_roles.get(uid, "chat")
    role_name = ROLES[role_key]["name"]
    if is_paid and sub_until:
        sub_text = f"✅ Активна до: {sub_until.strftime('%d.%m.%Y %H:%M')}"
    elif sub_until and not is_paid:
        sub_text = f"❌ Истекла: {sub_until.strftime('%d.%m.%Y %H:%M')}"
    else:
        sub_text = f"🆓 Бесплатный план ({FREE_LIMIT - min(free_used, FREE_LIMIT)} запросов осталось)"
    await message.answer(
        f"👤 Профиль\n\n"
        f"🤖 Модель: Claude Sonnet\n"
        f"🎭 Режим: {role_name}\n"
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
    if row and row["is_paid"] and row["subscription_until"] and row["subscription_until"] > datetime.utcnow():
        new_until = row["subscription_until"] + timedelta(days=30)
    else:
        new_until = datetime.utcnow() + timedelta(days=30)
    await db.execute("UPDATE users SET is_paid = TRUE, subscription_until = $1 WHERE user_id = $2", new_until, uid)
    until_str = new_until.strftime('%d.%m.%Y %H:%M')
    await bot.send_message(uid,
        f"✅ Подписка активирована!\n"
        f"📅 Действует до: {until_str}\n\n"
        f"Пользуйтесь без ограничений!")
    await callback.message.edit_text(callback.message.text + f"\n\n✅ Одобрено до {until_str}")
    await callback.answer("Подписка активирована!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split("_")[1])
    await bot.send_message(uid, "❌ Заявка отклонена. Напишите администратору @polyakovkonst для уточнения.")
    await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
    await callback.answer("Заявка отклонена")

@dp.message(F.text == "/stats")
async def stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_users = await db.fetchval("SELECT COUNT(*) FROM users")
    paid_users = await db.fetchval("SELECT COUNT(*) FROM users WHERE is_paid = TRUE")
    total_requests = await db.fetchval("SELECT SUM(total_requests) FROM users")
    top_users = await db.fetch("SELECT username, total_requests FROM users ORDER BY total_requests DESC LIMIT 5")
    top_text = "\n".join([f"@{r['username']} — {r['total_requests']} запросов" for r in top_users])
    await message.answer(
        f"📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💰 Платных: {paid_users}\n"
        f"📨 Всего запросов: {total_requests or 0}\n\n"
        f"🏆 Топ-5 пользователей:\n{top_text}"
    )

@dp.message(F.text == "/clear")
async def clear(message: Message):
    conversations.pop(message.from_user.id, None)
    await message.answer(
        "🗑 История диалога очищена!\n\n"
        "Это полезно когда хочешь начать новую тему с чистого листа — предыдущий контекст больше не влияет на ответы бота."
    )

@dp.message(F.text == "/support")
async def support(message: Message):
    await message.answer(
        "🆘 Поддержка\n\n"
        "Если у вас возникли вопросы или проблемы — напишите администратору:\n"
        "@polyakovkonst"
    )

async def handle_with_access(message: Message, content):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    free_used, is_paid, sub_until = await get_user(uid, username)
    if uid != ADMIN_ID and not is_paid and free_used >= FREE_LIMIT:
        await message.answer(
            "Бесплатные запросы закончились.\n"
            "Напиши /subscribe чтобы оформить подписку — 299 руб./мес."
        )
        return False
    await increment_usage(uid)
    if uid not in conversations:
        conversations[uid] = []
    conversations[uid].append({"role": "user", "content": content})
    if len(conversations[uid]) > MAX_HISTORY * 2:
        conversations[uid] = conversations[uid][-MAX_HISTORY * 2:]
    return True

@dp.message(F.photo)
async def handle_photo(message: Message):
    uid = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")
    photo = message.photo[-1]
    file_data = await download_file(photo.file_id)
    b64 = base64.standard_b64encode(file_data).decode("utf-8")
    caption = message.caption or "Опиши что на этом изображении"
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": caption}
    ]
    allowed = await handle_with_access(message, content)
    if not allowed:
        return
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=get_system_prompt(uid),
        messages=conversations[uid]
    )
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await message.answer(reply)

@dp.message(F.document)
async def handle_document(message: Message):
    uid = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")
    doc = message.document
    mime = doc.mime_type or ""
    name = doc.file_name or ""
    caption = message.caption or "Проанализируй этот файл"
    file_data = await download_file(doc.file_id)
    if mime == "application/pdf":
        b64 = base64.standard_b64encode(file_data).decode("utf-8")
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": caption}
        ]
    elif mime.startswith("image/"):
        b64 = base64.standard_b64encode(file_data).decode("utf-8")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
            {"type": "text", "text": caption}
        ]
    else:
        try:
            if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or name.endswith(".docx"):
                import io
                from docx import Document
                doc_obj = Document(io.BytesIO(file_data))
                text = "\n".join([p.text for p in doc_obj.paragraphs])
            elif mime in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel") or name.endswith((".xlsx", ".xls")):
                import io
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(file_data))
                text = ""
                for sheet in wb.sheetnames:
                    ws = wb[sheet]
                    text += f"\n[Лист: {sheet}]\n"
                    for row in ws.iter_rows(values_only=True):
                        text += "\t".join([str(c) if c is not None else "" for c in row]) + "\n"
            else:
                text = file_data.decode("utf-8", errors="ignore")
            content = f"{caption}\n\nСодержимое файла «{name}»:\n\n{text[:15000]}"
        except Exception as e:
            await message.answer(f"Не удалось прочитать файл: {e}")
            return
    allowed = await handle_with_access(message, content)
    if not allowed:
        return
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=get_system_prompt(uid),
        messages=conversations[uid]
    )
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await message.answer(reply)

@dp.message()
async def handle(message: Message):
    uid = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")
    allowed = await handle_with_access(message, message.text)
    if not allowed:
        return
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=get_system_prompt(uid),
        messages=conversations[uid]
    )
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await message.answer(reply, reply_markup=ReplyKeyboardRemove())

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
