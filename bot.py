import asyncio
import os
import re
import json
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

PROXY_URL = "http://user406008:5o06f2@138.249.26.121:5009"

ADMIN_ID = 416065237
FREE_LIMIT = 5
CHANNEL_BONUS_LIMIT = 30
MAX_HISTORY = 10
CHANNEL_ID = "@probiznav"
EDITORIAL_CHAT_ID = -5220322973

PLANS = {
    "basic":    {"name": "Базовый",   "price": "150 ⭐️/мес.",  "stars": 150,  "requests": 200,  "file_mb_total": 50,  "file_mb_single": 10},
    "standard": {"name": "Стандарт",  "price": "250 ⭐️/мес.",  "stars": 250,  "requests": 500,  "file_mb_total": 150, "file_mb_single": 10},
    "pro":      {"name": "Про",       "price": "350 ⭐️/мес.",  "stars": 350,  "requests": 1000,  "file_mb_total": 500, "file_mb_single": 25},
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db: asyncpg.Pool = None
conversations = {}
user_roles = {}
posted_hashes = set()
pending_edits = {}
publish_queue = []

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

🆓 Бесплатно: {limit} запросов в день
🎁 Подпишись на @probiznav и получи 30 запросов в день бесплатно!

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
— Базовый: 150 ⭐️ — 200 запросов + 50 МБ файлов
— Стандарт: 250 ⭐️ — 500 запросов + 150 МБ файлов
— Про: 350 ⭐️ — безлимит запросов + 500 МБ файлов"""


# ─── Утилита: разбить длинный текст на части по 4096 символов ────────────────

async def claude_create_with_retry(max_retries=3, **kwargs):
    """Вызывает Claude API с повторными попытками при перегрузке (529)."""
    for attempt in range(max_retries):
        try:
            return ai.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < max_retries - 1:
                wait = 10 * (attempt + 1)
                print(f"Claude перегружен, повтор через {wait} сек... (попытка {attempt+1})")
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError("Claude недоступен после нескольких попыток")


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Разбивает текст на части, стараясь не резать посередине абзаца."""
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # ищем последний перенос строки в пределах max_len
        cut = text.rfind('\n', 0, max_len)
        if cut == -1 or cut < max_len // 2:
            # нет удобного места — режем по пробелу
            cut = text.rfind(' ', 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    return parts


async def send_long_message(message: Message, text: str, **kwargs):
    """Отправляет текст, разбивая на части если он длиннее 4096 символов."""
    parts = split_message(text)
    for part in parts:
        await message.answer(part, **kwargs)
        if len(parts) > 1:
            await asyncio.sleep(0.3)  # небольшая пауза между сообщениями


# ─── Утилита: сериализация/десериализация контента для БД ────────────────────

def content_to_str(content) -> str:
    """Сохраняет content (строку или список) в строку для БД."""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def str_to_content(s: str):
    """Восстанавливает content из строки БД."""
    try:
        val = json.loads(s)
        if isinstance(val, (list, dict)):
            return val
    except Exception:
        pass
    return s


# ─────────────────────────────────────────────────────────────────────────────

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            user_id BIGINT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
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
    await db.execute("""
        CREATE TABLE IF NOT EXISTS posted_links (
            hash TEXT PRIMARY KEY,
            posted_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            hash TEXT PRIMARY KEY,
            title TEXT,
            draft_text TEXT,
            link TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def get_user(user_id, username):
    row = await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if not row:
        await db.execute("INSERT INTO users (user_id, username) VALUES ($1, $2)", user_id, username)
        return await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if row["is_paid"] and row["subscription_until"] and row["subscription_until"] < datetime.now(timezone.utc).replace(tzinfo=None):
        await db.execute("UPDATE users SET is_paid = FALSE, plan = 'free', requests_used = 0, file_mb_used = 0 WHERE user_id = $1", user_id)
        await bot.send_message(user_id, "⚠️ Ваша подписка истекла. Напишите /subscribe чтобы продлить.")
        return await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if row["is_paid"] and row["period_start"]:
        period_end = row["period_start"] + timedelta(days=30)
        if datetime.now(timezone.utc).replace(tzinfo=None) > period_end:
            await db.execute("UPDATE users SET requests_used = 0, file_mb_used = 0, period_start = $1 WHERE user_id = $2", datetime.now(timezone.utc).replace(tzinfo=None), user_id)
            return await db.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    return row

async def check_limits(user_id, username, file_mb=0):
    if user_id == ADMIN_ID:
        return True, None
    row = await get_user(user_id, username)
    plan_key = row["plan"] or "free"
    if not row["is_paid"]:
        today = datetime.now(timezone.utc).replace(tzinfo=None).date()
        reset_date = row["free_reset_date"] if row["free_reset_date"] else None
        if reset_date != today:
            await db.execute("UPDATE users SET free_used = 0, free_reset_date = $1 WHERE user_id = $2", today, user_id)
            free_used = 0
        else:
            free_used = row["free_used"]
        subscribed = await is_subscribed(user_id)
        daily_limit = CHANNEL_BONUS_LIMIT if subscribed else FREE_LIMIT
        if free_used >= daily_limit:
            if subscribed:
                return False, (
                    f"На сегодня запросы закончились ({daily_limit}/день).\n"
                    "Возвращайся завтра или напиши /subscribe для безлимитного доступа."
                )
            else:
                return False, (
                    f"На сегодня бесплатные запросы закончились ({FREE_LIMIT}/день).\n\n"
                    f"🎁 Подпишись на канал @probiznav и получи 30 запросов в день бесплатно!\n\n"
                    "Или напиши /subscribe для полного доступа."
                )
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
    if row and row["is_paid"] and row["subscription_until"] and row["subscription_until"] > datetime.now(timezone.utc).replace(tzinfo=None):
        new_until = row["subscription_until"] + timedelta(days=30)
    else:
        new_until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
    await db.execute("""
        UPDATE users SET is_paid = TRUE, subscription_until = $1, plan = $2,
        requests_used = 0, file_mb_used = 0, period_start = $3 WHERE user_id = $4
    """, new_until, plan_key, datetime.now(timezone.utc).replace(tzinfo=None), user_id)
    return new_until

async def parse_site(url):
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin, urlparse
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers, proxy=PROXY_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'advertisement']):
            tag.decompose()
        stop_words = [
            'реклама', 'advert', 'banner', 'promo', 'sponsor', 'партнер',
            'подписка', 'subscribe', 'login', 'register', 'вакансии', 'jobs',
            'about', 'contact', 'policy', 'terms', 'cookie', 'help',
            'теги', 'tags', 'category', 'author', 'profile', 'поиск',
            'facebook', 'twitter', 'vk.com', 'instagram', 't.me',
        ]
        articles = []
        for a in soup.find_all('a', href=True):
            title = a.get_text(strip=True)
            href = a['href']
            if len(title) < 40 or len(title) > 200:
                continue
            title_lower = title.lower()
            href_lower = href.lower()
            skip = False
            for word in stop_words:
                if word in title_lower or word in href_lower:
                    skip = True
                    break
            if skip:
                continue
            if not href.startswith('http'):
                href = urljoin(url, href)
            base_domain = urlparse(url).netloc
            link_domain = urlparse(href).netloc
            if base_domain not in link_domain and link_domain not in base_domain:
                continue
            articles.append({"title": title, "link": href})
        seen = set()
        unique = []
        for a in articles:
            if a['link'] not in seen:
                seen.add(a['link'])
                unique.append(a)
        return unique[:10]
    except Exception as e:
        print(f"Parse error {url}: {e}")
        return []

async def get_article_text(url):
    try:
        from bs4 import BeautifulSoup
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers, proxy=PROXY_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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

async def prepare_draft(title, text, link):
    prompt = (
        f"Перепиши эту новость для Telegram-канала о бизнесе и налогах. "
        f"Аудитория — предприниматели МСП. "
        f"Требования: без смайликов и эмодзи, только текст. "
        f"Добавь один хештег в конце через # (например #налоги или #МСП). Не более 4 предложений.\n\n"
        f"Заголовок: {title}\n"
        f"Текст: {text[:1500]}"
    )
    response = await claude_create_with_retry(
        model="claude-sonnet-4-5-20251022",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    post_text = response.content[0].text
    post_text = re.sub(r'\*\*?(.*?)\*\*?', r'\1', post_text)
    post_text = re.sub(r'^#{1,6}\s', '', post_text, flags=re.MULTILINE)
    return post_text

async def save_draft(title, draft_text, link):
    h = hashlib.md5(link.encode()).hexdigest()
    await db.execute("INSERT INTO posted_links (hash) VALUES ($1) ON CONFLICT DO NOTHING", h)
    await db.execute("""
        INSERT INTO drafts (hash, title, draft_text, link, status)
        VALUES ($1, $2, $3, $4, 'pending')
        ON CONFLICT (hash) DO UPDATE SET draft_text = $3, status = 'pending'
    """, h, title, draft_text, link)
    return h

async def send_for_approval(draft_id, draft_text, link):
    preview = f"📝 Черновик на утверждение:\n\n{draft_text}\n\nИсточник: {link}"
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data=f"pub_{draft_id}")
    builder.button(text="✏️ Редактировать", callback_data=f"edit_{draft_id}")
    builder.button(text="❌ Пропустить", callback_data=f"skip_{draft_id}")
    builder.adjust(3)
    await bot.send_message(EDITORIAL_CHAT_ID, preview, reply_markup=builder.as_markup())

async def collect_candidates():
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
        "https://www.klerk.ru/news/",
        "https://www.audit-it.ru/news/",
        "https://www.garant.ru/news/",
        "https://corpmsp.ru/press-centr/news/",
        "https://deloros.ru/news/",
    ]
    candidates = []
    for site_url in sites:
        try:
            articles = await parse_site(site_url)
            for article in articles[:3]:
                h = hashlib.md5(article['link'].encode()).hexdigest()
                exists = await db.fetchval("SELECT hash FROM posted_links WHERE hash = $1", h)
                if not exists:
                    candidates.append(article)
        except Exception as e:
            print(f"Collect error {site_url}: {e}")
    return candidates

async def pick_top_candidates(candidates, n=3):
    if not candidates:
        return []
    if len(candidates) <= n:
        return candidates
    titles = "\n".join([f"{i+1}. {c['title']}" for i, c in enumerate(candidates[:20])])
    prompt = (
        f"Ты редактор Telegram-канала для предпринимателей МСП. "
        f"Выбери {n} самых важных и интересных новости из списка ниже. "
        f"Критерии: налоги, льготы, законодательство, банки, господдержка МСП. "
        f"Ответь ТОЛЬКО номерами через запятую, например: 2, 5, 8\n\n"
        f"{titles}"
    )
    response = await claude_create_with_retry(
        model="claude-sonnet-4-5-20251022",
        max_tokens=20,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    )
    try:
        nums = [int(x.strip()) - 1 for x in response.content[0].text.strip().split(",")]
        result = [candidates[i] for i in nums if 0 <= i < len(candidates)]
        return result[:n]
    except:
        return candidates[:n]

async def fetch_and_post():
    candidates = await collect_candidates()
    if not candidates:
        print("Нет новых кандидатов")
        return
    top3 = await pick_top_candidates(candidates, n=3)
    if not top3:
        return
    for candidate in top3:
        text = await get_article_text(candidate['link'])
        if not text or len(text) < 100:
            continue
        draft = await prepare_draft(candidate['title'], text, candidate['link'])
        if not draft:
            continue
        draft_id = await save_draft(candidate['title'], draft, candidate['link'])
        await send_for_approval(draft_id, draft, candidate['link'])
        await asyncio.sleep(5)  # пауза между запросами к Claude чтобы не упасть в rate limit

async def process_publish_queue():
    while True:
        if publish_queue:
            draft_id = publish_queue.pop(0)
            row = await db.fetchrow("SELECT * FROM drafts WHERE hash = $1", draft_id)
            if row and row["status"] == "queued":
                post_text = row["draft_text"]
                post_text += f'\n\n<a href="{row["link"]}">Читать источник</a>'
                post_text += f"\n@probiznav"
                try:
                    await bot.send_message(CHANNEL_ID, post_text, parse_mode="HTML", disable_web_page_preview=True)
                    await db.execute("UPDATE drafts SET status = 'published' WHERE hash = $1", draft_id)
                except Exception as e:
                    print(f"Ошибка публикации: {e}")
            if publish_queue:
                await asyncio.sleep(20 * 60)
        await asyncio.sleep(30)

async def scheduler():
    import random
    last_collect = 0
    while True:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        msk_hour = (now.hour + 3) % 24
        current_time = now.timestamp()
        if current_time - last_collect > 1800:
            await collect_candidates()
            last_collect = current_time
        if 8 <= msk_hour < 24:
            wait_minutes = random.randint(45, 75)
            await asyncio.sleep(wait_minutes * 60)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            msk_hour = (now.hour + 3) % 24
            if 8 <= msk_hour < 24:
                await fetch_and_post()
        else:
            await asyncio.sleep(600)

@dp.message(F.text == "/start")
async def start(message: Message):
    conversations.pop(message.from_user.id, None)
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Наш канал @probiznav", url="https://t.me/probiznav")
    builder.adjust(1)
    await message.answer(WELCOME_TEXT.format(limit=FREE_LIMIT), reply_markup=builder.as_markup())

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
        subscribed = await is_subscribed(uid)
        daily_limit = CHANNEL_BONUS_LIMIT if subscribed else FREE_LIMIT
        sub_text = (
            f"🆓 Бесплатный план ({daily_limit - min(row['free_used'], daily_limit)} запросов осталось сегодня)\n"
            f"{'✅ Подписан на канал — бонус 30 запросов/день' if subscribed else '📢 Подпишись на @probiznav — получи 30 запросов/день'}"
        )
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
        "🔹 Базовый — 299 руб./мес.(150⭐️) \n200 запросов + 50 МБ файлов\n\n"
        "🔸 Стандарт — 499 руб./мес.(250⭐️) \n500 запросов + 150 МБ файлов\n\n"
        "💎 Про — 699 руб./мес.(350⭐️) \nБезлимит запросов + 500 МБ файлов"
    )
    builder = InlineKeyboardBuilder()
    for key, plan in PLANS.items():
        builder.button(text=f"{plan['name']} — {plan['price']}", callback_data=f"buy_{key}")
    builder.button(text="🤝 Тестовый доступ", callback_data=f"test_{uid}")
    builder.adjust(1)
    await message.answer("👇 Выбери тариф:", reply_markup=builder.as_markup())
    await message.answer("💬 Если есть вопросы по тарифам — обращайтесь к администратору: @polyakovkonst")

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

@dp.callback_query(F.data.startswith("pub_"))
async def publish_draft(callback: CallbackQuery):
    draft_id = callback.data.split("_", 1)[1]
    row = await db.fetchrow("SELECT * FROM drafts WHERE hash = $1", draft_id)
    if not row:
        await callback.answer("Черновик не найден")
        return
    if publish_queue:
        publish_queue.append(draft_id)
        await db.execute("UPDATE drafts SET status = 'queued' WHERE hash = $1", draft_id)
        pos = len(publish_queue)
        wait_min = (pos - 1) * 20
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏳ Добавлено в очередь (публикация через ~{wait_min} мин.)"
        )
        await callback.answer("Добавлено в очередь!")
    else:
        post_text = row["draft_text"]
        post_text += f'\n\n<a href="{row["link"]}">Читать источник</a>'
        post_text += f"\n@probiznav"
        await bot.send_message(CHANNEL_ID, post_text, parse_mode="HTML", disable_web_page_preview=True)
        await db.execute("UPDATE drafts SET status = 'published' WHERE hash = $1", draft_id)
        await callback.message.edit_text(callback.message.text + "\n\n✅ Опубликовано!")
        await callback.answer("Опубликовано в канал!")

@dp.callback_query(F.data.startswith("edit_"))
async def edit_draft(callback: CallbackQuery):
    draft_id = callback.data.split("_", 1)[1]
    row = await db.fetchrow("SELECT * FROM drafts WHERE hash = $1", draft_id)
    if not row:
        await callback.answer("Черновик не найден")
        return
    pending_edits[callback.from_user.id] = draft_id
    await callback.message.reply(
        f"✏️ Отправь исправленный текст в ответ на это сообщение.\n\n"
        f"Текущий черновик:\n\n{row['draft_text']}"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("skip_"))
async def skip_draft(callback: CallbackQuery):
    draft_id = callback.data.split("_", 1)[1]
    await db.execute("UPDATE drafts SET status = 'skipped' WHERE hash = $1", draft_id)
    await callback.message.edit_text(callback.message.text + "\n\n❌ Пропущено")
    await callback.answer("Черновик пропущен")

@dp.message(F.reply_to_message & F.chat.id == EDITORIAL_CHAT_ID)
async def receive_edited_draft(message: Message):
    uid = message.from_user.id
    draft_id = pending_edits.get(uid)
    if not draft_id:
        return
    row = await db.fetchrow("SELECT * FROM drafts WHERE hash = $1", draft_id)
    if not row:
        return
    new_text = message.text
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data=f"pub_{draft_id}")
    builder.button(text="❌ Отменить", callback_data=f"skip_{draft_id}")
    builder.adjust(2)
    await db.execute("UPDATE drafts SET draft_text = $1 WHERE hash = $2", new_text, draft_id)
    await message.answer(
        f"📝 Обновлённый черновик:\n\n{new_text}\n\nИсточник: {row['link']}",
        reply_markup=builder.as_markup()
    )
    del pending_edits[uid]

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
    uid = message.from_user.id
    conversations.pop(uid, None)
    await db.execute("DELETE FROM conversations WHERE user_id = $1", uid)
    await message.answer("🗑 История диалога очищена!\n\nЭто полезно когда хочешь начать новую тему с чистого листа.")

@dp.message(F.text == "/support")
async def support(message: Message):
    await message.answer(
        "🆘 Поддержка\n\n"
        "Если у вас возникли вопросы — напишите администратору:\n@polyakovkonst\n\n"
        "📄 Публичная оферта: https://telegra.ph/Publichnaya-oferta-River-first-bot-04-27"
    )

@dp.message(F.text == "/postnow")
async def post_now(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔄 Запускаю публикацию новостей...")
    await fetch_and_post()
    await message.answer("✅ Черновики отправлены в редакторский чат!")

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
        draft = await prepare_draft(title, text, url)
        draft_id = await save_draft(title, draft, url)
        await send_for_approval(draft_id, draft, url)
        await message.answer("✅ Черновик отправлен в редакторский чат!")
    except asyncio.TimeoutError:
        await message.answer("❌ Сайт не ответил за 20 секунд.")
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
        rows = await db.fetch(
            "SELECT role, content FROM conversations WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            uid, MAX_HISTORY * 2
        )
        # ФИX: восстанавливаем content из JSON если это был список (фото/документ)
        conversations[uid] = [
            {"role": r["role"], "content": str_to_content(r["content"])}
            for r in reversed(rows)
        ]
    # ФИX: сохраняем в БД через json.dumps если content — список
    content_str = content_to_str(content)
    await db.execute(
        "INSERT INTO conversations (user_id, role, content) VALUES ($1, $2, $3)",
        uid, "user", content_str
    )
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
    response = await claude_create_with_retry(
        model="claude-sonnet-4-5-20251022",
        max_tokens=1500,
        system=get_system_prompt(uid),
        messages=conversations[uid]
    )
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    await db.execute("INSERT INTO conversations (user_id, role, content) VALUES ($1, $2, $3)", uid, "assistant", reply)
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await send_long_message(message, reply)


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
    response = await claude_create_with_retry(
        model="claude-sonnet-4-5-20251022",
        max_tokens=1500,
        system=get_system_prompt(uid),
        messages=conversations[uid]
    )
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    await db.execute("INSERT INTO conversations (user_id, role, content) VALUES ($1, $2, $3)", uid, "assistant", reply)
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await send_long_message(message, reply)


@dp.message()
async def handle(message: Message):
    uid = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")
    allowed = await handle_with_access(message, message.text)
    if not allowed:
        return
    response = await claude_create_with_retry(
        model="claude-sonnet-4-5-20251022",
        max_tokens=1500,
        system=get_system_prompt(uid),
        messages=conversations[uid]
    )
    reply = response.content[0].text
    conversations[uid].append({"role": "assistant", "content": reply})
    await db.execute("INSERT INTO conversations (user_id, role, content) VALUES ($1, $2, $3)", uid, "assistant", reply)
    reply = re.sub(r'\*\*?(.*?)\*\*?', r'\1', reply)
    reply = re.sub(r'#{1,6}\s?', '', reply)
    await send_long_message(message, reply, reply_markup=ReplyKeyboardRemove())


async def main():
    await init_db()
    asyncio.create_task(scheduler())
    asyncio.create_task(process_publish_queue())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
