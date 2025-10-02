import os
import logging
import random
import re
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
import sqlite3
import asyncio  # ← добавлено для set_webhook

# --- Экранирование для MarkdownV2 ---
def escape_md(text: str) -> str:
    """Экранирует спецсимволы для MarkdownV2 в Telegram."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
DB_NAME = 'marriage_bot.db'
TOKEN = "8471148948:AAEoMjY0C79NjisPoz6mJRhCabntCI-SIm8"  # ← только для локального теста!
# TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # ← Будет задан в Render
#
# if not TOKEN:
#     raise ValueError("TELEGRAM_BOT_TOKEN не установлен!")

# --- Создаём Flask-приложение ---
app = Flask(__name__)

# --- Инициализация Telegram Application (без запуска polling) ---
telegram_app = Application.builder().token(TOKEN).build()

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Браки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS marriages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER NOT NULL,
            user2 INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            married_at TEXT DEFAULT (datetime('now')),
            budget INTEGER DEFAULT 0,
            last_daily TEXT,
            family_level INTEGER DEFAULT 1,
            UNIQUE(user1, chat_id),
            UNIQUE(user2, chat_id),
            CHECK(user1 != user2)
        )
    ''')

    # Дети
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS children (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent1 INTEGER,
            parent2 INTEGER,
            chat_id INTEGER,
            name TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            birthday TEXT DEFAULT (datetime('now', '+1 year'))
        )
    ''')

    # Предложения (анти-спам)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS proposals (
            user_id INTEGER,
            chat_id INTEGER,
            timestamp TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    ''')

    # Пользователи (работа)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            job TEXT DEFAULT 'Безработный',
            work_streak INTEGER DEFAULT 0,
            last_work TEXT,
            total_works INTEGER DEFAULT 0
        )
    ''')

    # Квесты
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quests (
            user_id INTEGER,
            chat_id INTEGER,
            quest_type TEXT,
            target INTEGER,
            progress INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id, quest_type)
        )
    ''')

    # Магазин
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY,
            name TEXT,
            type TEXT,
            price INTEGER,
            description TEXT
        )
    ''')

    # Заполнение магазина
    cursor.execute('SELECT COUNT(*) FROM shop_items')
    if cursor.fetchone()[0] == 0:
        items = [
            ('Кассир', 'job', 100, 'Работает в магазине'),
            ('Повар', 'job', 200, 'Готовит еду'),
            ('Учитель', 'job', 300, 'Учит детей'),
            ('Программист', 'job', 500, 'Пишет код'),
            ('Блогер', 'job', 400, 'Снимает видео'),
            ('Кольцо', 'gift', 150, 'Подарок супругу'),
            ('Дом', 'upgrade', 1000, 'Даёт пассивный доход +20 за ход'),
        ]
        cursor.executemany('INSERT INTO shop_items (name, type, price, description) VALUES (?, ?, ?, ?)', items)

    # Проверка колонки birthday и family_level
    cursor.execute("PRAGMA table_info(children)")
    cols = [c[1] for c in cursor.fetchall()]
    if 'birthday' not in cols:
        cursor.execute("ALTER TABLE children ADD COLUMN birthday TEXT DEFAULT (datetime('now', '+1 year'))")

    cursor.execute("PRAGMA table_info(marriages)")
    cols = [c[1] for c in cursor.fetchall()]
    if 'family_level' not in cols:
        cursor.execute("ALTER TABLE marriages ADD COLUMN family_level INTEGER DEFAULT 1")

    conn.commit()
    conn.close()

# --- Получить имя пользователя ---
async def get_name(update: Update, user_id: int) -> str:
    try:
        user = await update.get_bot().get_chat(user_id)
        return user.full_name or user.username or f"Пользователь {user_id}"
    except:
        return f"Пользователь {user_id}"

# --- Проверка брака ---
def is_married(user_id: int, chat_id: int) -> tuple:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user1, user2, married_at, budget, last_daily, family_level FROM marriages
        WHERE (user1 = ? OR user2 = ?) AND chat_id = ?
    ''', (user_id, user_id, chat_id))
    row = cursor.fetchone()
    conn.close()
    return row

# --- Регистрация брака ---
def register_marriage(user1: int, user2: int, chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM marriages WHERE user1 = ? OR user2 = ?', (user1, user1))
        cursor.execute('DELETE FROM marriages WHERE user1 = ? OR user2 = ?', (user2, user2))
        cursor.execute('''
            INSERT INTO marriages (user1, user2, chat_id, married_at, budget, last_daily, family_level)
            VALUES (?, ?, ?, datetime('now'), 0, NULL, 1)
        ''', (user1, user2, chat_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при регистрации брака: {e}")
        conn.rollback()
    finally:
        conn.close()

# --- Расторжение брака ---
def divorce(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM marriages WHERE user1 = ? OR user2 = ?', (user_id, user_id))
    conn.commit()
    conn.close()

# --- Обновить бюджет семьи ---
def update_family_budget(user_id: int, chat_id: int, amount: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE marriages SET budget = budget + ?
        WHERE (user1 = ? OR user2 = ?) AND chat_id = ?
    ''', (amount, user_id, user_id, chat_id))
    conn.commit()
    conn.close()

# --- Получить бюджет ---
def get_family_budget(user_id: int, chat_id: int) -> int:
    marriage = is_married(user_id, chat_id)
    return marriage[3] if marriage else 0

# --- Можно ли предложить брак ---
def can_propose(user_id: int, chat_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT timestamp FROM proposals WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return True
    last = datetime.fromisoformat(row[0])
    return datetime.now() - last > timedelta(seconds=300)

# --- Обновить время предложения ---
def update_proposal_time(user_id: int, chat_id: int):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO proposals (user_id, chat_id, timestamp)
        VALUES (?, ?, ?)
    ''', (user_id, chat_id, now))
    conn.commit()
    conn.close()

# --- Количество детей ---
def count_children(user_id: int, chat_id: int) -> int:
    marriage = is_married(user_id, chat_id)
    if not marriage:
        return 0
    u1, u2 = marriage[0], marriage[1]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) FROM children
        WHERE ((parent1 = ? AND parent2 = ?) OR (parent1 = ? AND parent2 = ?)) AND chat_id = ?
    ''', (u1, u2, u2, u1, chat_id))
    return cursor.fetchone()[0]

# --- Получить детей ---
def get_children(user_id: int, chat_id: int):
    marriage = is_married(user_id, chat_id)
    if not marriage:
        return []
    u1, u2 = marriage[0], marriage[1]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT name, created_at, birthday FROM children
        WHERE ((parent1 = ? AND parent2 = ?) OR (parent1 = ? AND parent2 = ?)) AND chat_id = ?
    ''', (u1, u2, u2, u1, chat_id))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- Уровни семьи ---
FAMILY_LEVELS = [
    (0, "🌱 Новички"),
    (500, "🏡 Молодая семья"),
    (1500, "👨‍👩‍👧 Дом с ребёнком"),
    (3000, "🏰 Успешная семья"),
    (5000, "👑 Аристократы")
]

def get_family_level(budget: int, kids: int) -> tuple:
    score = budget + kids * 200
    level = 1
    title = "🌱 Новички"
    for i, (threshold, name) in enumerate(FAMILY_LEVELS):
        if score >= threshold:
            level = i + 1
            title = name
    return level, title

def update_family_level(user_id: int, chat_id: int) -> tuple:
    budget = get_family_budget(user_id, chat_id)
    kids = count_children(user_id, chat_id)
    new_level, title = get_family_level(budget, kids)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT family_level FROM marriages WHERE (user1 = ? OR user2 = ?) AND chat_id = ?', (user_id, user_id, chat_id))
    row = cursor.fetchone()
    old_level = row[0] if row else 1

    if new_level > old_level:
        cursor.execute('UPDATE marriages SET family_level = ? WHERE (user1 = ? OR user2 = ?) AND chat_id = ?', (new_level, user_id, user_id, chat_id))
        conn.commit()
        conn.close()
        return new_level, title, True
    conn.close()
    return new_level, title, False

# --- Достижения ---
def get_achievements(user_id: int, chat_id: int) -> list:
    ach = []
    marriage = is_married(user_id, chat_id)
    if not marriage:
        return ["🌟 Начни с /marry!"]

    days = (datetime.now() - datetime.fromisoformat(marriage[2])).days
    kids = count_children(user_id, chat_id)
    budget = get_family_budget(user_id, chat_id)

    if days >= 365:
        ach.append("🎖️ Годовщина: вместе больше года!")
    if kids >= 1:
        ach.append("👶 Первая семья: у вас есть ребёнок!")
    if kids >= 3:
        ach.append("👨‍👩‍👧‍👦 Многодетная семья: 3+ детей!")
    if budget >= 1000:
        ach.append("🏦 Богачи: бюджет ≥ 1000 монет")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT quest_type FROM quests WHERE user_id = ? AND chat_id = ? AND completed = 1', (user_id, chat_id))
    completed = [row[0] for row in cursor.fetchall()]
    conn.close()

    if 'work_5_times' in completed:
        ach.append("👷‍♂️ Трудяга: завершил квест 'Работать 5 раз'")
    if 'earn_500' in completed:
        ach.append("💰 Финансист: заработал 500 монет")
    if 'have_child' in completed:
        ach.append("❤️ Родитель: завёл ребёнка")

    return ach or ["💞 Молодожёны"]

# --- РАБОТА И КВЕСТЫ ---
JOBS = ["Безработный", "Кассир", "Повар", "Учитель", "Программист", "Блогер"]
JOB_SALARY = {
    "Безработный": 10, "Кассир": 30, "Повар": 40,
    "Учитель": 50, "Программист": 100, "Блогер": 70
}
PASSIVE_INCOME = {"Дом": 20}

QUESTS_INFO = {
    "work_5_times": {"desc": "Работать 5 раз", "target": 5, "reward": 200},
    "earn_500": {"desc": "Заработать 500 монет", "target": 500, "reward": 300},
    "have_child": {"desc": "Завести ребёнка", "target": 1, "reward": 150},
    "be_married_30_days": {"desc": "Быть в браке 30 дней", "target": 30, "reward": 400}
}

def get_user(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT job, work_streak, last_work, total_works FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    row = cursor.fetchone()
    conn.close()
    return row

def create_user(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, chat_id, job, work_streak, last_work, total_works)
        VALUES (?, ?, 'Безработный', 0, NULL, 0)
    ''', (user_id, chat_id))
    conn.commit()
    conn.close()

def update_job(user_id: int, chat_id: int, job: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET job = ? WHERE user_id = ? AND chat_id = ?', (job, user_id, chat_id))
    conn.commit()
    conn.close()

def update_work_stats(user_id: int, chat_id: int, streak: int, total: int):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET work_streak = ?, total_works = ?, last_work = ?
        WHERE user_id = ? AND chat_id = ?
    ''', (streak, total, now, user_id, chat_id))
    conn.commit()
    conn.close()

def get_quest(user_id: int, chat_id: int, quest_type: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT progress, completed FROM quests WHERE user_id = ? AND chat_id = ? AND quest_type = ?', (user_id, chat_id, quest_type))
    row = cursor.fetchone()
    conn.close()
    return row

def create_quest(user_id: int, chat_id: int, quest_type: str):
    quest = QUESTS_INFO.get(quest_type)
    if not quest:
        return
    target = quest["target"]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO quests (user_id, chat_id, quest_type, target, progress, completed)
        VALUES (?, ?, ?, ?, 0, 0)
    ''', (user_id, chat_id, quest_type, target))
    conn.commit()
    conn.close()

def update_quest_progress(user_id: int, chat_id: int, quest_type: str, progress: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE quests SET progress = ? WHERE user_id = ? AND chat_id = ? AND quest_type = ?', (progress, user_id, chat_id, quest_type))
    conn.commit()
    conn.close()

def complete_quest_db(user_id: int, chat_id: int, quest_type: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE quests SET completed = 1, progress = target WHERE user_id = ? AND chat_id = ? AND quest_type = ?', (user_id, chat_id, quest_type))
    conn.commit()
    conn.close()

def get_shop():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT name, type, price, description FROM shop_items')
    rows = cursor.fetchall()
    conn.close()
    return rows

def buy_item(user_id: int, chat_id: int, item_name: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT price, type FROM shop_items WHERE name = ?', (item_name,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    price, item_type = row

    budget = get_family_budget(user_id, chat_id)
    if budget < price:
        conn.close()
        return False

    update_family_budget(user_id, chat_id, -price)
    if item_type == 'job':
        update_job(user_id, chat_id, item_name)
    conn.close()
    return True

def reset_user(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM marriages WHERE user1 = ? OR user2 = ?', (user_id, user_id))
        cursor.execute('DELETE FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
        cursor.execute('DELETE FROM quests WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при сбросе пользователя {user_id}: {e}")
        conn.rollback()
    finally:
        conn.close()

# --- КОМАНДЫ ---

WELCOME_MSG = (
    "🏡 *Добро пожаловать в «Семейную RPG»!* 🌟\n\n"
    "Здесь ты можешь:\n"
    "• 💍 Создать семью\n"
    "• 👶 Завести детей\n"
    "• 💼 Работать и расти по карьерной лестнице\n"
    "• 🏦 Копить бюджет семьи\n"
    "• 🎯 Выполнять квесты и получать награды\n"
    "• 🏠 Покупать улучшения в магазине\n"
    "• 📈 Повышать уровень семьи\n\n"
    "Всё начинается с одного шага — найди свою вторую половинку!\n\n"
    "✨ *Команды:* ✨\n"
    "• `/marry` — создать семью\n"
    "• `/work` — работать и зарабатывать\n"
    "• `/profile` — посмотреть свой профиль\n"
    "• `/shop` — магазин профессий и улучшений\n"
    "• `/daily` — получить ежедневный бонус\n"
    "• `/child` — завести ребёнка\n"
    "• `/divorce` — развестись\n"
    "• `/reset` — начать сначала\n\n"
    "💬 *Совет:* Чем дольше вы вместе, тем выше уровень семьи и больше бонусов!"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(escape_md(WELCOME_MSG), parse_mode='MarkdownV2')

# --- /marry ---
async def marry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(escape_md("Только в группах!"), parse_mode='MarkdownV2')
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if is_married(user_id, chat_id):
        await update.message.reply_text(escape_md("Ты уже в браке!"), parse_mode='MarkdownV2')
        return

    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text(escape_md("Используй: /marry и ответь на сообщение пользователя."), parse_mode='MarkdownV2')
        return

    target_user = update.message.reply_to_message.from_user
    target_id = target_user.id

    if target_id == user_id:
        await update.message.reply_text(escape_md("Нельзя жениться на себе!"), parse_mode='MarkdownV2')
        return

    if is_married(target_id, chat_id):
        await update.message.reply_text(escape_md("Твой избранник уже в браке!"), parse_mode='MarkdownV2')
        return

    if not can_propose(user_id, chat_id):
        await update.message.reply_text(escape_md("Подожди 5 минут перед следующим предложением."), parse_mode='MarkdownV2')
        return

    update_proposal_time(user_id, chat_id)
    sender_name = await get_name(update, user_id)
    receiver_name = await get_name(update, target_id)

    keyboard = [
        [InlineKeyboardButton("💍 Принять", callback_data=f"marry_accept:{user_id}:{target_id}:{chat_id}"),
         InlineKeyboardButton("💔 Отклонить", callback_data=f"marry_reject:{user_id}:{target_id}:{chat_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"💍 {sender_name} делает предложение {receiver_name}!\nСогласен(-на)?"
    await update.message.reply_text(escape_md(text), reply_markup=reply_markup, parse_mode='MarkdownV2')

async def marry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(":")
    action = data[0]
    user_id = int(data[1])
    target_id = int(data[2])
    chat_id = int(data[3])

    if query.from_user.id != target_id:
        await query.answer("Это не тебе предложение!", show_alert=True)
        return

    if action == "marry_accept":
        register_marriage(user_id, target_id, chat_id)
        husband = await get_name(update, user_id)
        wife = await get_name(update, target_id)
        text = f"🎉 Поздравляем! {husband} и {wife} теперь в браке! 💍"
        await query.edit_message_text(escape_md(text), parse_mode='MarkdownV2')
        create_quest(user_id, chat_id, "have_child")
        create_quest(target_id, chat_id, "have_child")

    elif action == "marry_reject":
        sender = await get_name(update, user_id)
        text = f"💔 {sender} был отклонён..."
        await query.edit_message_text(escape_md(text), parse_mode='MarkdownV2')

    await query.answer()

# --- /reset ---
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    keyboard = [
        [InlineKeyboardButton("✅ Да, сбросить", callback_data=f"reset_confirm:{user_id}:{chat_id}"),
         InlineKeyboardButton("❌ Нет", callback_data="reset_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "⚠️ *Внимание!*\n"
        "Это действие сбросит весь твой прогресс:\n"
        "• Удалит брак\n"
        "• Обнулит работу и квесты\n\n"
        "Ты уверен?"
    )
    await update.message.reply_text(escape_md(text), reply_markup=reply_markup, parse_mode='MarkdownV2')

async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(":")

    if data[0] == "reset_cancel":
        await query.edit_message_text(escape_md("❌ Сброс отменён."), parse_mode='MarkdownV2')
        await query.answer()
        return

    if data[0] != "reset_confirm":
        await query.answer()
        return

    user_id = int(data[1])
    chat_id = int(data[2])

    if query.from_user.id != user_id:
        await query.answer("Это не ты запускал сброс!", show_alert=True)
        return

    reset_user(user_id, chat_id)
    await query.edit_message_text(escape_md("✅ Твой прогресс сброшен. Добро пожаловать в новую жизнь!"), parse_mode='MarkdownV2')
    await query.answer()

# --- /work ---
async def work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    create_user(user_id, chat_id)
    user = get_user(user_id, chat_id)
    if not user:
        return
    job, _, last_work, total_works = user

    if last_work:
        last = datetime.fromisoformat(last_work)
        if datetime.now() - last < timedelta(hours=6):
            wait = 6 - int((datetime.now() - last).total_seconds() / 3600)
            await update.message.reply_text(escape_md(f"⏳ Подожди {wait} ч."), parse_mode='MarkdownV2')
            return

    salary = JOB_SALARY.get(job, 10)
    event = ""

    if random.random() < 0.2:
        evt_name, mult = random.choice([("Повышен!", 1.5), ("Премия!", 2.0)])
        salary = int(salary * mult)
        event = f"\n🎁 Событие: *{evt_name}*"

    if get_family_budget(user_id, chat_id) >= 1000:
        passive = PASSIVE_INCOME["Дом"]
        update_family_budget(user_id, chat_id, passive)
        event += f"\n🏠 Пассивный доход: +{passive}"

    new_streak = user[1] + 1 if last_work and datetime.now() - datetime.fromisoformat(last_work) < timedelta(days=1) else 1
    new_total = total_works + 1
    update_work_stats(user_id, chat_id, new_streak, new_total)
    update_family_budget(user_id, chat_id, salary)

    create_quest(user_id, chat_id, "work_5_times")
    quest = get_quest(user_id, chat_id, "work_5_times")
    if quest and not quest[1]:
        progress = min(quest[0] + 1, 5)
        update_quest_progress(user_id, chat_id, "work_5_times", progress)
        if progress >= 5:
            reward = QUESTS_INFO["work_5_times"]["reward"]
            update_family_budget(user_id, chat_id, reward)
            complete_quest_db(user_id, chat_id, "work_5_times")
            event += f"\n🏆 Квест завершён! +{reward} монет!"

    await update.message.reply_text(
        escape_md(f"💼 Работал как {job}: +{salary} монет{event}\n🔥 Серия: {new_streak}"),
        parse_mode='MarkdownV2'
    )

# --- /quests ---
async def quests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    create_user(user_id, chat_id)
    for q_type in QUESTS_INFO:
        create_quest(user_id, chat_id, q_type)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT quest_type, progress, completed, target FROM quests WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    rows = cursor.fetchall()
    conn.close()

    text = "🎯 *Твои квесты:*\n\n"
    for q_type, progress, completed, target in rows:
        quest = QUESTS_INFO.get(q_type, {})
        desc = quest.get("desc", q_type)
        status = "✅" if completed else "🔄"
        p = min(progress, target)
        text += f"{status} *{desc}*: `{p}/{target}`"
        if completed:
            text += " (награда получена)"
        text += "\n"

    await update.message.reply_text(escape_md(text), parse_mode='MarkdownV2')

# --- /shop ---
async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_shop()
    text = "🛒 *Магазин:*\n\n"
    for name, item_type, price, desc in items:
        emoji = "👔" if item_type == "job" else "🎁" if item_type == "gift" else "🏠"
        text += f"{emoji} *{name}* — `{price}` монет\n{desc}\n\n"
    text += "Покупай: `/buy Название`"
    await update.message.reply_text(escape_md(text), parse_mode='MarkdownV2')

# --- /buy ---
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(escape_md("Укажи: /buy Кассир"), parse_mode='MarkdownV2')
        return
    item_name = " ".join(context.args)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if buy_item(user_id, chat_id, item_name):
        await update.message.reply_text(escape_md(f"✅ Куплено: {item_name}!"), parse_mode='MarkdownV2')
        if item_name in JOB_SALARY:
            await update.message.reply_text(escape_md(f"💼 Теперь ты {item_name}!"), parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(escape_md("❌ Не хватает денег или нет такого."), parse_mode='MarkdownV2')

# --- /profile ---
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_name = await get_name(update, user_id)
    create_user(user_id, chat_id)
    user = get_user(user_id, chat_id)
    job, streak, _, total_works = user if user else ("Безработный", 0, None, 0)

    marriage = is_married(user_id, chat_id)
    kids = count_children(user_id, chat_id)
    budget = get_family_budget(user_id, chat_id)
    achievements = get_achievements(user_id, chat_id)
    ach_text = "\n".join([f"🔹 {a}" for a in achievements])

    status = "💍 В браке" if marriage else "👤 Холост(а)"
    married_to = ""
    level_info = ""
    if marriage:
        partner_id = marriage[1] if marriage[0] == user_id else marriage[0]
        partner_name = await get_name(update, partner_id)
        days = (datetime.now() - datetime.fromisoformat(marriage[2])).days
        level, title, _ = update_family_level(user_id, chat_id)
        level_info = f"\n• Уровень семьи: {level} — {title}"
        married_to = f"\n• Партнёр: {partner_name}\n• Вместе: {days} дней"

    text = (
        f"🌟 Профиль: {user_name}\n\n"
        f"📌 Статус: {status}{married_to}{level_info}\n"
        f"💼 Работа: {job}\n"
        f"🔥 Серия работ: {streak} дней\n"
        f"👷‍♂️ Всего работ: {total_works}\n"
        f"👶 Детей: {kids}\n"
        f"💰 Бюджет: {budget} монет\n\n"
        f"🏆 Достижения:\n{ach_text}"
    )
    await update.message.reply_text(escape_md(text), parse_mode='MarkdownV2')

# --- /daily ---
async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    create_user(user_id, chat_id)
    marriage = is_married(user_id, chat_id)
    if not marriage:
        await update.message.reply_text(escape_md("Только для супругов!"), parse_mode='MarkdownV2')
        return

    last_daily_str = marriage[4]
    if last_daily_str:
        last = datetime.fromisoformat(last_daily_str)
        if datetime.now() - last < timedelta(days=1):
            await update.message.reply_text(escape_md("Подожди до завтра!"), parse_mode='MarkdownV2')
            return

    amount = 50
    if get_family_budget(user_id, chat_id) >= 1000:
        amount = 100

    update_family_budget(user_id, chat_id, amount)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE marriages SET last_daily = datetime("now") WHERE (user1 = ? OR user2 = ?) AND chat_id = ?', (user_id, user_id, chat_id))
    conn.commit()
    conn.close()

    new_level, title, level_up = update_family_level(user_id, chat_id)
    bonus = f"\n🎉 Повышен до уровня {new_level}: {title}!" if level_up else ""

    await update.message.reply_text(escape_md(f"🎁 Ежедневный бонус: +{amount} монет!{bonus}"), parse_mode='MarkdownV2')

# --- /casino ---
async def casino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_married(user_id, chat_id):
        await update.message.reply_text(escape_md("Только для супругов!"), parse_mode='MarkdownV2')
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(escape_md("Используй: /casino <сумма>"), parse_mode='MarkdownV2')
        return

    try:
        bet = int(context.args[0])
    except:
        await update.message.reply_text(escape_md("Введите число."), parse_mode='MarkdownV2')
        return

    budget = get_family_budget(user_id, chat_id)
    if bet > budget:
        await update.message.reply_text(escape_md("Недостаточно монет!"), parse_mode='MarkdownV2')
        return
    if bet < 10:
        await update.message.reply_text(escape_md("Минимальная ставка — 10."), parse_mode='MarkdownV2')
        return

    if random.random() < 0.6:
        win = bet * 2
        update_family_budget(user_id, chat_id, win - bet)
        result = f"🎉 Вы выиграли {win} монет!"
    else:
        update_family_budget(user_id, chat_id, -bet)
        result = f"💸 Проиграли {bet} монет..."

    await update.message.reply_text(escape_md(f"🎲 Казино: {result}"), parse_mode='MarkdownV2')

# --- /gift ---
async def gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    marriage = is_married(user_id, chat_id)
    if not marriage:
        await update.message.reply_text(escape_md("Ты не в браке!"), parse_mode='MarkdownV2')
        return

    if not context.args:
        await update.message.reply_text(escape_md("Используй: /gift Кольцо"), parse_mode='MarkdownV2')
        return

    item_name = " ".join(context.args)
    if item_name != "Кольцо":
        await update.message.reply_text(escape_md("Пока можно дарить только Кольцо (150 монет)."), parse_mode='MarkdownV2')
        return

    if get_family_budget(user_id, chat_id) < 150:
        await update.message.reply_text(escape_md("Недостаточно монет!"), parse_mode='MarkdownV2')
        return

    update_family_budget(user_id, chat_id, -150)
    partner_id = marriage[1] if marriage[0] == user_id else marriage[0]
    sender = await get_name(update, user_id)
    receiver = await get_name(update, partner_id)
    await update.message.reply_text(escape_md(f"🎁 {sender} подарил(а) кольцо {receiver}! 💍"), parse_mode='MarkdownV2')

# --- /child ---
async def child(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    marriage = is_married(user_id, chat_id)
    if not marriage:
        await update.message.reply_text(escape_md("Только для супругов!"), parse_mode='MarkdownV2')
        return

    kids = count_children(user_id, chat_id)
    if kids >= 5:
        await update.message.reply_text(escape_md("У вас уже много детей!"), parse_mode='MarkdownV2')
        return

    if get_family_budget(user_id, chat_id) < 100:
        await update.message.reply_text(escape_md("Нужно 100 монет на воспитание!"), parse_mode='MarkdownV2')
        return

    update_family_budget(user_id, chat_id, -100)
    u1, u2 = marriage[0], marriage[1]
    name = f"Ребёнок-{random.randint(100, 999)}"
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO children (parent1, parent2, chat_id, name)
        VALUES (?, ?, ?, ?)
    ''', (u1, u2, chat_id, name))
    conn.commit()
    conn.close()

    if get_quest(user_id, chat_id, "have_child") and not get_quest(user_id, chat_id, "have_child")[1]:
        reward = QUESTS_INFO["have_child"]["reward"]
        update_family_budget(user_id, chat_id, reward)
        complete_quest_db(user_id, chat_id, "have_child")
        await update.message.reply_text(escape_md(f"👶 У вас родился {name}!\n🏆 Квест завершён! +{reward} монет!"), parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(escape_md(f"👶 У вас родился {name}!"), parse_mode='MarkdownV2')

# --- /divorce ---
async def divorce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_married(user_id, chat_id):
        await update.message.reply_text(escape_md("Ты и так свободен!"), parse_mode='MarkdownV2')
        return

    divorce(user_id, chat_id)
    await update.message.reply_text(escape_md("💔 Вы развелись..."), parse_mode='MarkdownV2')

# --- Регистрация обработчиков ---
def register_handlers():
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("marry", marry))
    telegram_app.add_handler(CommandHandler("work", work))
    telegram_app.add_handler(CommandHandler("quests", quests))
    telegram_app.add_handler(CommandHandler("shop", shop))
    telegram_app.add_handler(CommandHandler("buy", buy))
    telegram_app.add_handler(CommandHandler("profile", profile))
    telegram_app.add_handler(CommandHandler("daily", daily))
    telegram_app.add_handler(CommandHandler("casino", casino))
    telegram_app.add_handler(CommandHandler("gift", gift))
    telegram_app.add_handler(CommandHandler("child", child))
    telegram_app.add_handler(CommandHandler("divorce", divorce_cmd))
    telegram_app.add_handler(CommandHandler("reset", reset))

    telegram_app.add_handler(CallbackQueryHandler(marry_callback, pattern=r"^marry_"))
    telegram_app.add_handler(CallbackQueryHandler(reset_callback, pattern=r"^reset_"))

# --- Webhook endpoint ---
@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return 'OK', 200

# --- Health check (Render будет пинговать /) ---
@app.route('/', methods=['GET'])
def home():
    return 'Marriage Bot is running on Render! ✅', 200

# --- Установка webhook при запуске сервера ---
def set_webhook():
    hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
    if not hostname:
        # Для локального тестирования раскомментируйте и укажите ваш ngrok URL:
        # hostname = "abcd-123-45-67.ngrok.io"  # ← ЗАМЕНИТЕ НА ВАШ NGROK URL!
        raise ValueError("RENDER_EXTERNAL_HOSTNAME не установлен! Для локального запуска задайте hostname вручную.")

    webhook_url = f"https://{hostname}/webhook/{TOKEN}"
    logger.info(f"Setting webhook to: {webhook_url}")
    asyncio.run(telegram_app.bot.set_webhook(url=webhook_url))

# --- Запуск приложения ---
if __name__ == '__main__':
    init_db()
    register_handlers()

    # Устанавливаем webhook
    set_webhook()

    # Запускаем Flask
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)