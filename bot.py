import os
import random
import sqlite3
import logging
import threading
import time
import urllib.request
from datetime import timezone, timedelta, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from datetime import time as dtime

TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "bot.db")
MSK = timezone(timedelta(hours=3))

logging.basicConfig(level=logging.INFO)

# ─── Keep-alive ───────────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_ping_server():
    server = HTTPServer(("0.0.0.0", 8080), PingHandler)
    server.serve_forever()

def keep_alive():
    url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
    while True:
        try:
            urllib.request.urlopen(f"{url}/ping")
        except:
            pass
        time.sleep(600)

# ─── База данных ──────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id   INTEGER PRIMARY KEY,
            chat_name TEXT
        );

        CREATE TABLE IF NOT EXISTS participants (
            user_id     INTEGER,
            chat_id     INTEGER,
            username    TEXT,
            first_name  TEXT,
            is_selected INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS daily_pick (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER,
            user_id      INTEGER,
            message_id   INTEGER,
            pick_date    TEXT,
            votes_marry  INTEGER DEFAULT 0,
            votes_slap   INTEGER DEFAULT 0,
            votes_fuck   INTEGER DEFAULT 0,
            votes_ignore INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS votes (
            pick_id   INTEGER,
            user_id   INTEGER,
            action    TEXT,
            PRIMARY KEY (pick_id, user_id)
        );
    """)
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB_PATH)

# ─── Регистрация чата ─────────────────────────────────────────────────────────

def register_chat(chat_id: int, chat_name: str):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO chats (chat_id, chat_name)
        VALUES (?, ?)
    """, (chat_id, chat_name))
    conn.commit()
    conn.close()

def get_all_chat_ids() -> list[int]:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM chats")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ─── Логика выбора без повторений ─────────────────────────────────────────────

def pick_next_participant(chat_id: int):
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT user_id, username, first_name
        FROM participants
        WHERE chat_id = ? AND is_selected = 0
    """, (chat_id,))
    remaining = c.fetchall()

    if not remaining:
        conn.execute(
            "UPDATE participants SET is_selected = 0 WHERE chat_id = ?",
            (chat_id,)
        )
        conn.commit()
        c.execute("""
            SELECT user_id, username, first_name
            FROM participants WHERE chat_id = ?
        """, (chat_id,))
        remaining = c.fetchall()

    if not remaining:
        conn.close()
        return None

    chosen = random.choice(remaining)
    user_id, username, first_name = chosen

    conn.execute("""
        UPDATE participants SET is_selected = 1
        WHERE user_id = ? AND chat_id = ?
    """, (user_id, chat_id))
    conn.commit()
    conn.close()

    return {"user_id": user_id, "username": username, "first_name": first_name}

# ─── Ежедневный выбор в 9:00 ─────────────────────────────────────────────────

async def daily_pick_job(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = get_all_chat_ids()

    for chat_id in chat_ids:
        participant = pick_next_participant(chat_id)
        if not participant:
            continue

        name = f"@{participant['username']}" if participant['username'] \
               else participant['first_name']

        text = f"🎲 Участник дня: {name}\n\nЧто с ним/ней сделаем?"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💍 Жениться (0)",     callback_data="marry|0"),
                InlineKeyboardButton("👋 Дать чапалах (0)", callback_data="slap|0"),
            ],
            [
                InlineKeyboardButton("🔥 Трахнуть (0)",     callback_data="fuck|0"),
                InlineKeyboardButton("🙄 Игнор (0)",         callback_data="ignore|0"),
            ]
        ])

        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard
            )
            conn = get_conn()
            conn.execute("""
                INSERT INTO daily_pick (chat_id, user_id, message_id, pick_date)
                VALUES (?, ?, ?, ?)
            """, (chat_id, participant['user_id'], msg.message_id, str(date.today())))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"Ошибка отправки в чат {chat_id}: {e}")

# ─── Напоминание в 21:00 ─────────────────────────────────────────────────────

async def evening_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = get_all_chat_ids()

    for chat_id in chat_ids:
        conn = get_conn()
        c = conn.cursor()

        # Берём сегодняшний выбор
        c.execute("""
            SELECT p.username, p.first_name
            FROM daily_pick d
            JOIN participants p ON p.user_id = d.user_id AND p.chat_id = d.chat_id
            WHERE d.chat_id = ? AND d.pick_date = ?
            ORDER BY d.id DESC LIMIT 1
        """, (chat_id, str(date.today())))
        row = c.fetchone()
        conn.close()

        if not row:
            continue

        username, first_name = row
        name = f"@{username}" if username else first_name

        text = (
            f"Ну что, {name}, сегодня мы узнаем насколько тебя любят в чате 👀\n\n"
            f"Пришли скрин с результатами!"
        )

        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logging.error(f"Ошибка напоминания в чат {chat_id}: {e}")

# ─── Обработка нажатий кнопок ────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    message_id = query.message.message_id
    chat_id = query.message.chat_id
    action, _ = query.data.split("|")

    column_map = {
        "marry": "votes_marry",
        "slap":  "votes_slap",
        "fuck":  "votes_fuck",
        "ignore":"votes_ignore",
    }
    col = column_map.get(action)
    if not col:
        await query.answer()
        return

    conn = get_conn()
    c = conn.cursor()

    # Находим pick_id для этого сообщения
    c.execute("""
        SELECT id FROM daily_pick
        WHERE message_id = ? AND chat_id = ?
    """, (message_id, chat_id))
    pick_row = c.fetchone()

    if not pick_row:
        await query.answer()
        conn.close()
        return

    pick_id = pick_row[0]

    # Проверяем — голосовал ли уже этот пользователь
    c.execute("""
        SELECT action FROM votes
        WHERE pick_id = ? AND user_id = ?
    """, (pick_id, user_id))
    existing = c.fetchone()

    if existing:
        old_action = existing[0]

        if old_action == action:
            # Нажал ту же кнопку — ничего не делаем
            await query.answer("Ты уже выбрал этот вариант", show_alert=False)
            conn.close()
            return

        # Нажал другую кнопку — меняем голос
        old_col = column_map[old_action]
        conn.execute(f"""
            UPDATE daily_pick SET {old_col} = MAX(0, {old_col} - 1)
            WHERE id = ?
        """, (pick_id,))
        conn.execute(f"""
            UPDATE daily_pick SET {col} = {col} + 1
            WHERE id = ?
        """, (pick_id,))
        conn.execute("""
            UPDATE votes SET action = ?
            WHERE pick_id = ? AND user_id = ?
        """, (action, pick_id, user_id))

    else:
        # Первый голос
        conn.execute(f"""
            UPDATE daily_pick SET {col} = {col} + 1
            WHERE id = ?
        """, (pick_id,))
        conn.execute("""
            INSERT INTO votes (pick_id, user_id, action)
            VALUES (?, ?, ?)
        """, (pick_id, user_id, action))

    conn.commit()

    # Читаем обновлённые счётчики
    c.execute("""
        SELECT votes_marry, votes_slap, votes_fuck, votes_ignore
        FROM daily_pick WHERE id = ?
    """, (pick_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        await query.answer()
        return

    vm, vs, vf, vi = row

    # Тихо обновляем кнопки — без сообщения в чат
    new_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"💍 Жениться ({vm})",     callback_data=f"marry|{vm}"),
            InlineKeyboardButton(f"👋 Дать чапалах ({vs})", callback_data=f"slap|{vs}"),
        ],
        [
            InlineKeyboardButton(f"🔥 Трахнуть ({vf})",     callback_data=f"fuck|{vf}"),
            InlineKeyboardButton(f"🙄 Игнор ({vi})",         callback_data=f"ignore|{vi}"),
        ]
    ])

    await query.answer()  # убираем "часики" — без текста, тихо
    await query.edit_message_reply_markup(reply_markup=new_keyboard)

# ─── Бот добавлен в группу ───────────────────────────────────────────────────

async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            chat = update.effective_chat
            register_chat(chat.id, chat.title or "")
            await update.message.reply_text(
                "Привет! Я буду каждый день выбирать случайного участника. "
                "Просто общайтесь — я запомню всех кто пишет в чат."
            )

# ─── Регистрация участников ───────────────────────────────────────────────────

async def track_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        return

    register_chat(chat.id, chat.title or "")

    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO participants (user_id, chat_id, username, first_name)
        VALUES (?, ?, ?, ?)
    """, (user.id, chat.id, user.username, user.first_name))
    conn.commit()
    conn.close()

# ─── Статистика в личных сообщениях ──────────────────────────────────────────

async def private_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "private":
        return

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT p.first_name, p.username,
               d.votes_marry, d.votes_slap, d.votes_fuck, d.votes_ignore,
               d.pick_date
        FROM daily_pick d
        JOIN participants p ON p.user_id = d.user_id AND p.chat_id = d.chat_id
        ORDER BY d.id DESC LIMIT 1
    """)
    row = c.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("Ещё никто не выбран сегодня 🤷")
        return

    name, username, vm, vs, vf, vi, pick_date = row
    display = f"@{username}" if username else name
    total = vm + vs + vf + vi

    text = (
        f"📊 Статистика за {pick_date}\n"
        f"Участник дня: {display}\n\n"
        f"💍 Жениться:      {vm}\n"
        f"👋 Дать чапалах:  {vs}\n"
        f"🔥 Трахнуть:      {vf}\n"
        f"🙄 Игнор:         {vi}\n\n"
        f"Всего голосов: {total}\n\n"
        f"_Личности голосовавших не сохраняются_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()

    threading.Thread(target=run_ping_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    # Голосование в 9:00 МСК
    app.job_queue.run_daily(
        daily_pick_job,
        time=dtime(hour=9, minute=0, tzinfo=MSK),
    )

    # Напоминание в 21:00 МСК
    app.job_queue.run_daily(
        evening_reminder_job,
        time=dtime(hour=21, minute=0, tzinfo=MSK),
    )

    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, on_bot_added
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT, track_members
    ))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT, private_stats
    ))

    logging.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()