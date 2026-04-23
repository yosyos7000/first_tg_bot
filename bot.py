import asyncio
import os
import re
import asyncpg
import aiohttp
import base64
import hashlib
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import ReplyKeyboardRemove
import anthropic

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 416065237
FREE_LIMIT = 5
MAX_HISTORY = 10
CHANNEL_ID = "@probiznav"

PLANS = {
    "basic":    {"name": "Базовый",   "price": "299 руб./мес.",  "stars": 250,  "requests": 200,  "file_mb_total": 50,  "file_mb_single": 10},
    "standard": {"name": "Стандарт",  "price": "599 руб./мес.",  "stars": 500,  "requests": 500,  "file_mb_total": 150, "file_mb_single": 10},
    "pro":      {"name": "Про",       "price": "999 руб./мес.",  "stars": 830,  "requests": 99999,"file_mb_total": 500, "file_mb_single": 25},
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db = None
conversations = {}
user_roles = {}
posted_hashes = set()

ROLES = {
    "business":   {"name": "🧑‍💼 Бизнес-ассистент", "prompt": "Ты профессиональный бизнес-ассистент. Помогаешь с анализом данных, составлением документов, деловой перепиской, стратегическими решениями и бизнес-задачами. Отвечай чётко, структурированно и профессионально."},
    "copywriter": {"name": "✍️ Копирайтер",          "prompt": "Ты опытный копирайтер. Помогаешь писать тексты для соцсетей, рекламу, статьи, посты, описания товаров и услуг. Пиши живо, убедительно и цепляюще."},
    "chat":       {"name": "💬 Обычный чат",          "prompt": "Ты дружелюбный и умный собеседник. Отвечай естественно, помогай с любыми вопросами, будь полезным и приятным в общении."},
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
/profile — профиль и статус подписки
/role — выбрать режим работы
/clear — очистить историю диалога
/help — как пользоваться ботом
/subscribe — оформить подписку

У тебя есть {limit} бесплатных запросов каждый день.
Просто напиши свой вопрос или отправь файл! 👇"""

HELP_TEXT = """📖 Как пользоваться ботом

✏️ Текстовые запросы
Напиши свой вопрос — бот ответит. Можно задавать уточняющие вопросы, бот помнит контекст последних {history} сообщений.

📎 Файлы и фото
Отправь файл или фото. Бот умеет читать:
— Фотографии (JPG, PNG)
— Документы PDF
— Word файлы (.docx)
— Excel таблицы (.xlsx)
— Текстовые файлы (.txt)

🎭 Режимы работы (/role)
— 🧑‍💼 Бизнес-ассистент — деловые задачи, анализ, документы
— ✍️ Копирайтер — тексты, посты, реклама
— 💬 Обычный чат — общение и помощь по любым вопросам

🗑 Очистка контекста (/clear)
Бот помнит последние {history} сообщений. Используй /clear чтобы начать новую тему с чистого листа.

💰 Тарифы (/subscribe):
— Базовый: 250 ⭐️ — 200 запросов + 50 МБ файлов
— Стандарт: 500 ⭐️ — 500 запросов + 150 МБ файлов
— Про: 830 ⭐️ — безлимит запросов + 500 МБ файлов"""

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
            subscription_until TIMESTAMP,
            plan TEXT DEFAULT 'free',
            requests_used INTEGER DEFAULT 0,
            file_mb_used FLOAT DEFAULT 0,
            period_start TIMESTAMP
        )
    """)
    for col, defval in [
        ("total_requests", "0"), ("subscription_until", "NULL"),
        ("plan", "'free'"), ("requests_used", "0"),
        ("file_mb_used", "0"), ("period_start", "NULL")
    ]:
        try:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {'TEXT' if col == 'plan' else 'TIMESTAMP' if 'until' in col or 'start' in col else 'FLOAT' if 'mb' in col else 'INTEGER'} DEFAULT {defval}")
        except:
            pass
    await db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS free_reset_date DATE")

async def get_user(user_id, username):
    row = await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if not row:
        await db.execute("INSERT INTO users (user_id, username) VALUES ($1, $2)", user_id, username)
        return await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if row["is_paid"] and row["subscription_until"] and row["subscription_until"] < datetime.utcnow():
        await db.execute("UPDATE users SET is_paid = FALSE, plan = 'free', requests_used = 0, file_mb_used = 0 WHERE user_id = $1", user_id)
        await bot.send_message(user_id, "⚠️ Ваша подписка истекла. Напишите /subscribe чтобы продлить.")
        return await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if row["is_paid"] and row["period_start"]:
        period_end = row["period_start"] + timedelta(days=30)
        if datetime.utcnow() > period_end:
            await db.execute("UPDATE users SET requests_used = 0, file_mb_used = 0, period_start = $1 WHERE user_id = $2", datetime.utcnow(), user_id)
            return await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    return row

async def check_limits(user_id, username, file_mb=0):
    if user_id == ADMIN_ID:
        return True, None
    row = await get_user(user_id, username)
    plan_key = row["plan"] or "free"
    if not row["is_paid"]:
        today = datetime.utcnow().date()
        reset_date = row["free_reset_date"] if row["free_reset_date"] else None
        if reset_date != today:
            await db.execute("UPDATE users SET free_used = 0, free_reset_date = $1 WHERE user_id = $2", today, user_id)
            free_used = 0
        else:
            free_used = row["free_used"]
        if free_used >= FREE_LIMIT:
            return False, (f"На сегодня бесплатные запросы закончились ({FREE_LIMIT}/день).\nВозвращайся завтра или напиши /subscribe для безлимитного доступа.")
        return True, None
    plan = PLANS.get(plan_key, PLANS["basic"])
    if row["requests_used"] >= plan["requests"]:
        return False, f"Лимит запросов на этот месяц исчерпан ({plan['requests']} запросов).\nНапиши /subscribe для смены тарифа."
    if file_mb > 0:
        if file_mb > plan["file_mb_single"]:
            return False, f"Файл слишком большой. Максимальный размер: {plan['file_mb_single']} МБ."
        if (row["file_mb_used"] or 0) + file_mb > plan["file_mb_total"]:
            used = round(row["file_mb_used"] or 0, 1)
            return False, f"Превышен месячный лимит файлов. Использовано: {used} МБ из {plan['file_mb_total']} МБ."
    return True, None

async def increment_usage(user_id, file_mb=0):
    row = await db.fetchrow("SELECT is_paid, free_used FROM users WHERE user_id = $1", user_id)
    if not row["is_paid"]:
        await db.execute("UPDATE users SET free_used = free_used + 1, total_requests = total_requests + 1 WHERE user_id = $1", user_id)
    else:
        await db.execute("""
            UPDATE users SET requests_used = requests_used + 1,
            total_requests = total_requests + 1, file_mb_used = file_mb_used + $1
            WHERE user_id = $2
        """, file_mb, user_id)

def get_system_prompt(user_id):
    role_key = user_roles.get(user_id, "chat")
    return ROLES[role_key]["prompt"]

async def download_file(file_id):
    file = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()

async def activate_subscription(user_id, plan_key):
    plan = PLANS.get(plan_key, PLANS["basic"])
    row = await db.fetchrow("SELECT subscription_until, is_paid FROM users WHERE user_id = $1", user_id)
    if row and row["is_paid"] and row["subscription_until"] and row["subscription_until"] > datetime.utcnow():
        new_until = row["subscription_until"] + timedelta(days=30)
    else:
        new_until = datetime.utcnow() + timedelta(days=30)
    await db.execute("""
        UPDATE users SET is_paid = TRUE, subscription_until = $1, plan = $2,
        requests_used = 0, file_mb_used = 0, period_start = $3 WHERE user_id = $4
    """, new_until, plan_key, datetime.utcnow(), user_id)
    return new_until

async def parse_site(url):
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        articles = []
        for a in soup.find_all('a', href=True):
            title = a.get_text(strip=True)
            href = a['href']
            if len(title) > 30 and len(title) < 200:
                if not href.startswith('http'):
                    href = urljoin(url, href)
                articles.append({"title": title, "link": href})
        seen = set()
        unique = []
        for a in articles:
            if a['link'] not in seen:
                seen.add(a['link'])
                unique.append(a)
        return unique[:5]
    except Exception as e:
        print(f"Parse error {url}: {e}")
        return []

async def get_article_text(url):
    try:
        from bs4 import BeautifulSoup
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        for selector in ['article', '.article-body', '.content', 'main', '.text']:
            block = soup.select_one(selector)
            if block:
                return block.get_text(separator=' ', strip=True)[:3000]
        return soup.get_text(separator=' ', strip=True)[:3000]
    except Exception as e:
        print(f"Article error {url}: {e}")
        return None

async def rewrite_and_post(title, text, link):
    prompt = (
        f"Перепиши эту новость для Telegram-канала о бизнесе и налогах. "
        f"Аудитория — предприниматели МСП. "
        f"Требования: без смайликов и эмодзи, только текст. "
        f"Добавь один хештег в конце через # (например #налоги или #МСП). Не более 4 предложений.\n\n"
        f"Заголовок: {title}\n"
        f"Текст: {text[:1500]}"
    )
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    post_text = response.content[0].text
    post_text = re.sub(r'\*\*?(.*?)\*\*?', r'\1', post_text)
    post_text = re.sub(r'^#{1,6}\s', '', post_text, flags=re.MULTILINE)
    post_text += f'\n\n<a href="{link}">Читать источник</a>'
    post_text += f"\n@probiznav"
    await bot.send_message(CHANNEL_ID, post_text, parse_mode="HTML", disable_web_page_preview=True)

async def fetch_and_post():
    import random
    sites = [
        "https://www.rbc.ru/economics/",
        "https://www.kommersant.ru/finance",
        "https://www.vedomosti.ru/economics",
        "https://www.forbes.ru/finansy",
        "https://expert.ru/expert/",
        "https://www.dp.ru/a/economics/",
        "https://secretmag.ru/news/",
        "https://www.banki.ru/news/",
        "https://journal.tinkoff.ru/news/",
        "https://sovcombank.ru/blog",
        "https://nalog-nalog.ru/novosti/",
        "https://cbr.ru/press/event/",
        "https://minfin.gov.ru/ru/press-center/news/",
        "https://government.ru/news/",
        "https://mos.ru/news/",
    ]
    random.shuffle(sites)
    for site_url in sites:
        try:
            articles = await parse_site(site_url)
            for article in articles[:3]:
                h = hashlib.md5(article['link'].encode()).hexdigest()
                if h in posted_hashes:
                    continue
                posted_hashes.add(h)
                text = await get_article_text(article['link'])
                if not text or len(text) < 100:
                    continue
                await rewrite_and_post(article['title'], text, article['link'])
                return  # Публикуем только 1 новость и выходим
        except Exception as e:
            print(f"Site error {site_url}: {e}")

async def scheduler():
    last_posted_hour = -1
    while True:
        now = datetime.utcnow()
        if now.hour != last_posted_hour:
            last_posted_hour = now.hour
            await fetch_and_post()
        await asyncio.sleep(60)

@dp.message(F.text == "/start")
async def start(message: Message):
    conversations.pop(message.from_user.id, None)
    await message.answer(WELCOME_TEXT.format(limit=FREE_LIMIT), reply_markup=ReplyKeyboardRemove())

@dp.message(F.text == "/help")
async def help_cmd(message: Message):
    await message.answer(HELP_TEXT.format(history=MAX_HISTORY, limit=FREE_LIMIT))

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
        "🎭 Выбери режим работы:\n\n"
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
    await callback.message.edit_text(f"✅ Режим изменён на: {ROLES[role_key]['name']}\n\nИстория диалога очищена. Можешь начинать!")
    await callback.answer()

@dp.message(F.text.in_({"👤 Мой профиль", "/profile"}))
async def profile(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    row = await get_user(uid, username)
    role_name = ROLES.get(user_roles.get(uid, "chat"), ROLES["chat"])["name"]
    plan_key = row["plan"] or "free"
    if row["is_paid"] and row["subscription_until"]:
        plan = PLANS.get(plan_key, PLANS["basic"])
        req_limit = "∞" if plan["requests"] > 9999 else str(plan["requests"])
        sub_text = (
            f"✅ {plan['name']} — {plan['price']}\n"
            f"📅 До: {row['subscription_until'].strftime('%d.%m.%Y %H:%M')}\n"
            f"📨 Запросов: {row['requests_used']} / {req_limit}\n"
            f"📁 Файлов: {round(row['file_mb_used'] or 0, 1)} / {plan['file_mb_total']} МБ"
        )
    elif row["subscription_until"] and not row["is_paid"]:
        sub_text = f"❌ Истекла: {row['subscription_until'].strftime('%d.%m.%Y %H:%M')}"
    else:
        sub_text = f"🆓 Бесплатный план ({FREE_LIMIT - min(row['free_used'], FREE_LIMIT)} запросов осталось сегодня)"
    await message.answer(
        f"👤 Профиль\n\n"
        f"🤖 Модель: Claude Sonnet\n"
        f"🎭 Режим: {role_name}\n"
        f"📋 Подписка: {sub_text}\n"
        f"📊 Всего запросов за всё время: {row['total_requests']}"
    )

async def message_answer_safe(callback, text):
    try:
        await callback.message.edit_text(text)
    except:
        await callback.message.answer(text)

@dp.message(F.text == "/subscribe")
async def subscribe(message: Message):
    uid = message.from_user.id
    await message.answer(
        "💰 Выбери тариф:\n\n"
        "🔹 Базовый — 299 руб./мес.\n200 запросов + 50 МБ файлов\n\n"
        "🔸 Стандарт — 599 руб./мес.\n500 запросов + 150 МБ файлов\n\n"
        "💎 Про — 999 руб./мес.\nБезлимит запросов + 500 МБ файлов"
    )
    builder = InlineKeyboardBuilder()
    for key, plan in PLANS.items():
        builder.button(text=f"{plan['name']} — {plan['price']}", callback_data=f"buy_{key}")
    builder.button(text="🤝 Тестовый доступ", callback_data=f"test_{uid}")
    builder.adjust(1)
    await message.answer("👇 Выбери тариф:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("test_"))
async def test_access(callback: CallbackQuery):
    uid = int(callback.data.split("_")[1])
    username = callback.from_user.username or "без username"
    full_name = callback.from_user.full_name or "без имени"
    await message_answer_safe(callback, "✅ Заявка на тестовый доступ отправлена!\nАдминистратор свяжется с тобой.")
    builder = InlineKeyboardBuilder()
    for key, plan in PLANS.items():
        builder.button(text=f"✅ Одобрить «{plan['name']}»", callback_data=f"approve_{uid}_{key}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{uid}")
    builder.adjust(1)
    await bot.send_message(
        ADMIN_ID,
        f"🤝 Заявка на тестовый доступ!\n"
        f"👤 @{username}\n"
        f"📝 Имя: {full_name}\n"
        f"🆔 ID: {uid}",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def buy_plan(callback: CallbackQuery):
    plan_key = callback.data.split("_")[1]
    plan = PLANS.get(plan_key)
    if not plan:
        return
    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Подписка «{plan['name']}»",
        description=f"{plan['requests'] if plan['requests'] < 9999 else 'Безлимит'} запросов + {plan['file_mb_total']} МБ файлов на 30 дней\n({plan['price']})",
        payload=f"sub_{plan_key}",
        currency="XTR",
        prices=[LabeledPrice(label=plan['name'], amount=plan['stars'])]
    )

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    payload = message.successful_payment.invoice_payload
    plan_key = payload.replace("sub_", "")
    plan = PLANS.get(plan_key, PLANS["basic"])
    new_until = await activate_subscription(uid, plan_key)
    until_str = new_until.strftime('%d.%m.%Y %H:%M')
    req_limit = "∞" if plan["requests"] > 9999 else str(plan["requests"])
    await message.answer(
        f"✅ Оплата прошла успешно!\n"
        f"📦 Тариф: {plan['name']}\n"
        f"📨 Запросов в месяц: {req_limit}\n"
        f"📁 Файлов в месяц: {plan['file_mb_total']} МБ\n"
        f"📅 Действует до: {until_str}\n\n"
        f"Пользуйтесь без ограничений!"
    )
    await bot.send_message(
        ADMIN_ID,
        f"💰 Новая оплата через Stars!\n"
        f"👤 @{username}\n"
        f"🆔 ID: {uid}\n"
        f"📦 Тариф: {plan['name']} — {plan['stars']} ⭐️"
    )

@dp.callback_query(F.data.startswith("approve_"))
async def approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("Ошибка: старый формат кнопки")
        return
    try:
        uid = int(parts[1])
        plan_key = parts[2]
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")
        return
    plan = PLANS.get(plan_key, PLANS["basic"])
    new_until = await activate_subscription(uid, plan_key)
    until_str = new_until.strftime('%d.%m.%Y %H:%M')
    req_limit = "∞" if plan["requests"] > 9999 else str(plan["requests"])
    await bot.send_message(uid,
        f"✅ Подписка активирована!\n"
        f"📦 Тариф: {plan['name']} — {plan['price']}\n"
        f"📨 Запросов в месяц: {req_limit}\n"
        f"📁 Файлов в месяц: {plan['file_mb_total']} МБ\n"
        f"📅 Действует до: {until_str}\n\n"
        f"Пользуйтесь без ограничений!")
    await callback.message.edit_text(callback.message.text + f"\n\n✅ Одобрено: {plan['name']} до {until_str}")
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
    basic = await db.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'basic' AND is_paid = TRUE")
    standard = await db.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'standard' AND is_paid = TRUE")
    pro = await db.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'pro' AND is_paid = TRUE")
    total_requests = await db.fetchval("SELECT SUM(total_requests) FROM users")
    top_users = await db.fetch("SELECT username, total_requests FROM users ORDER BY total_requests DESC LIMIT 5")
    top_text = "\n".join([f"@{r['username']} — {r['total_requests']} запросов" for r in top_users])
    stars_revenue = (basic or 0) * 250 + (standard or 0) * 500 + (pro or 0) * 830
    await message.answer(
        f"📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💰 Платных: {paid_users}\n"
        f"  🔹 Базовый: {basic}\n"
        f"  🔸 Стандарт: {standard}\n"
        f"  💎 Про: {pro}\n"
        f"⭐️ Stars/мес.: ~{stars_revenue}\n"
        f"📨 Всего запросов: {total_requests or 0}\n\n"
        f"🏆 Топ-5 пользователей:\n{top_text}"
    )

@dp.message(F.text == "/clear")
async def clear(message: Message):
    conversations.pop(message.from_user.id, None)
    await message.answer("🗑 История диалога очищена!\n\nЭто полезно когда хочешь начать новую тему с чистого листа.")

@dp.message(F.text == "/support")
async def support(message: Message):
    await message.answer("🆘 Поддержка\n\nЕсли у вас возникли вопросы — напишите администратору:\n@polyakovkonst")

@dp.message(F.text == "/postnow")
async def post_now(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔄 Запускаю публикацию новостей...")
    await fetch_and_post()
    await message.answer("✅ Готово! Проверяй канал @probiznav")

@dp.message(F.text.startswith("/post"))
async def manual_post(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи ссылку: /post https://ссылка")
        return
    url = parts[1].strip()
    await message.answer("🔄 Читаю статью...")
    try:
        text = await asyncio.wait_for(get_article_text(url), timeout=20)
        if not text or len(text) < 100:
            await message.answer("❌ Не удалось прочитать статью — сайт не отдал текст или заблокировал запрос.")
            return
        title = url.split("/")[-2] or "Новость"
        await rewrite_and_post(title, text, url)
        await message.answer("✅ Опубликовано в канал!")
    except asyncio.TimeoutError:
        await message.answer("❌ Сайт не ответил за 20 секунд — скорее всего блокирует автоматические запросы.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

async def handle_with_access(message: Message, content, file_mb=0):
    uid = message.from_user.id
    username = message.from_user.username or "без username"
    allowed, error = await check_limits(uid, username, file_mb)
    if not allowed:
        await message.answer(error)
        return False
    await increment_usage(uid, file_mb)
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
    file_mb = len(file_data) / (1024 * 1024)
    b64 = base64.standard_b64encode(file_data).decode("utf-8")
    caption = message.caption or "Опиши что на этом изображении"
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": caption}
    ]
    allowed = await handle_with_access(message, content, file_mb)
    if not allowed:
        return
    response = ai.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=get_system_prompt(uid), messages=conversations[uid])
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
    file_mb = len(file_data) / (1024 * 1024)
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
                from docx import Document as DocxDoc
                doc_obj = DocxDoc(io.BytesIO(file_data))
                text = "\n".join([p.text for p in doc_obj.paragraphs])
            elif mime in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel") or name.endswith((".xlsx", ".xls")):
                import io, openpyxl
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
    allowed = await handle_with_access(message, content, file_mb)
    if not allowed:
        return
    response = ai.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=get_system_prompt(uid), messages=conversations[uid])
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
    response = ai.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=get_system_prompt(uid), messages=conversations[uid])
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await message.answer(reply, reply_markup=ReplyKeyboardRemove())

async def main():
    await init_db()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
