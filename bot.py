import asyncio
import functools
import html
import logging
import random
import sqlite3
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config

log = logging.getLogger("block311")
router = Router()
TZ = ZoneInfo(config.TIMEZONE)
FMT = "%Y-%m-%dT%H:%M"
ADMINS = config.ADMIN_IDS
NEED = config.REQUIRED_APPROVALS
MAX_REJECTS = len(ADMINS) - NEED + 1  # столько отказов - и 2 одобрений уже не набрать

STATUS_RU = {
    "pending": "⏳ на рассмотрении",
    "approved": "✅ одобрена",
    "rejected": "❌ отклонена",
    "cancelled": "🚫 отменена",
    "expired": "⌛ просрочена",
}

# ---------------------------------------------------------------- db

PG = bool(config.DATABASE_URL)  # на Render база Postgres, локально — файл SQLite

if PG:
    import psycopg
    from psycopg.rows import dict_row

    def _connect():
        return psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)

    conn = _connect()
else:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row


class _Rows:
    def __init__(self, rows, rowcount):
        self.rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None


def q(sql, *args):
    global conn
    if PG:
        sql = sql.replace("?", "%s")
        try:
            return conn.execute(sql, args)
        except psycopg.OperationalError:  # бесплатная база закрывает простаивающие соединения
            conn = _connect()
            return conn.execute(sql, args)
    cur = conn.execute(sql, args)
    if "RETURNING" in sql:  # sqlite не даёт коммитить, пока строки не прочитаны
        rows = cur.fetchall()
        conn.commit()
        return _Rows(rows, cur.rowcount)
    conn.commit()
    return cur


_ID = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
for _stmt in (
    f"""CREATE TABLE IF NOT EXISTS requests(
        id {_ID},
        user_id BIGINT, username TEXT, full_name TEXT,
        purpose TEXT, visit_at TEXT, people INTEGER,
        status TEXT DEFAULT 'pending', code TEXT,
        reminded INTEGER DEFAULT 0, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS votes(
        request_id BIGINT, admin_id BIGINT, approve INTEGER,
        PRIMARY KEY(request_id, admin_id))""",
    "CREATE TABLE IF NOT EXISTS admin_msgs(request_id BIGINT, admin_id BIGINT, message_id BIGINT)",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS blocked(user_id BIGINT PRIMARY KEY)",
):
    q(_stmt)


def accepting() -> bool:
    row = q("SELECT value FROM settings WHERE key='accepting'").fetchone()
    return row is None or row["value"] == "1"


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def is_admin(uid: int) -> bool:
    return uid in ADMINS


# ---------------------------------------------------------------- texts


def who(r) -> str:
    name = html.escape(r["full_name"] or "?")
    return f'<a href="tg://user?id={r["user_id"]}">{name}</a>' + (
        f" (@{html.escape(r['username'])})" if r["username"] else ""
    )


def fmt_dt(iso: str) -> str:
    return datetime.strptime(iso, FMT).strftime("%d.%m %H:%M")


def admin_text(r) -> str:
    votes = q("SELECT * FROM votes WHERE request_id=?", r["id"]).fetchall()
    lines = []
    for v in votes:
        lines.append(f"{'👍' if v['approve'] else '👎'} {html.escape(admin_name(v['admin_id']))}")
    tally = "\n".join(lines) or "пока никто не голосовал"
    return (
        f"📨 <b>Заявка #{r['id']}</b> — {STATUS_RU[r['status']]}\n"
        f"👤 {who(r)}\n"
        f"🕒 {fmt_dt(r['visit_at'])}, человек: {r['people']}\n"
        f"🎯 {html.escape(r['purpose'])}\n\n"
        f"Нужно {NEED} одобрения из {len(ADMINS)}.\n{tally}"
    )


def admin_name(uid: int) -> str:
    return config.ADMIN_NAMES.get(uid, str(uid))


def vote_kb(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="👍 Одобрить", callback_data=f"v:{rid}:1"),
            InlineKeyboardButton(text="👎 Отклонить", callback_data=f"v:{rid}:0"),
        ]]
    )


# ---------------------------------------------------------------- guest flow


class Form(StatesGroup):
    purpose = State()
    day = State()
    time = State()
    people = State()
    confirm = State()


@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Это бот блока 311. У нас карантин: зайти можно только по одобренной заявке.\n\n"
        "/request — подать заявку\n"
        "/my — мои заявки\n"
        "/cancel — отменить текущее оформление\n\n"
        f"Заявку рассматривают {len(ADMINS)} жильцов, нужно {NEED} одобрения. "
        "После одобрения ты получишь код — покажи его у двери."
    )


@router.message(Command("cancel"))
async def cancel_form(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Окей, отменил оформление.")


@router.message(Command("request"))
async def request_cmd(m: Message, state: FSMContext):
    uid = m.from_user.id
    if q("SELECT 1 FROM blocked WHERE user_id=?", uid).fetchone():
        return await m.answer("Тебе сюда нельзя 🙅")
    if not accepting():
        return await m.answer("⛔ Сейчас заявки не принимаем. Попробуй позже.")
    if q("SELECT COUNT(*) c FROM requests WHERE user_id=? AND status='pending'", uid).fetchone()["c"]:
        return await m.answer("У тебя уже есть заявка на рассмотрении. Дождись решения или отмени её в /my.")
    day_start = now().replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S")
    if q("SELECT COUNT(*) c FROM requests WHERE user_id=? AND created_at>=?", uid, day_start).fetchone()["c"] >= config.MAX_REQUESTS_PER_DAY:
        return await m.answer(f"Лимит — {config.MAX_REQUESTS_PER_DAY} заявки в день. Приходи завтра.")
    await state.set_state(Form.purpose)
    await m.answer("По какому вопросу хочешь зайти? Напиши коротко (тема / зачем).")


@router.message(Form.purpose, F.text)
async def got_purpose(m: Message, state: FSMContext):
    if len(m.text) < 3 or len(m.text) > 300:
        return await m.answer("Опиши от 3 до 300 символов.")
    await state.update_data(purpose=m.text.strip())
    today = now().date()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=("Сегодня" if i == 0 else "Завтра" if i == 1 else (today + timedelta(days=i)).strftime("%d.%m")),
                             callback_data=f"day:{i}")
        for i in range(3)
    ]])
    await state.set_state(Form.day)
    await m.answer("Когда?", reply_markup=kb)


@router.callback_query(Form.day, F.data.startswith("day:"))
async def got_day(c: CallbackQuery, state: FSMContext):
    day = now().date() + timedelta(days=int(c.data.split(":")[1]))
    await state.update_data(day=day.isoformat())
    await state.set_state(Form.time)
    await c.message.edit_text(
        f"Дата: {day.strftime('%d.%m')}. Теперь время в формате <b>ЧЧ:ММ</b> "
        f"(принимаем с {config.VISIT_FROM} до {config.VISIT_TO})."
    )
    await c.answer()


def parse_hm(s: str) -> dtime:
    return datetime.strptime(s, "%H:%M").time()


@router.message(Form.time, F.text)
async def got_time(m: Message, state: FSMContext):
    try:
        t = parse_hm(m.text.strip().replace(".", ":"))
    except ValueError:
        return await m.answer("Не понял время. Пример: 17:00")
    data = await state.get_data()
    visit = datetime.combine(datetime.fromisoformat(data["day"]).date(), t)
    if visit < now() + timedelta(minutes=config.MIN_LEAD_MINUTES):
        return await m.answer(f"Заявку нужно подавать минимум за {config.MIN_LEAD_MINUTES} мин до визита. Другое время?")
    if not (parse_hm(config.VISIT_FROM) <= t <= parse_hm(config.VISIT_TO)):
        return await m.answer(f"Мы принимаем с {config.VISIT_FROM} до {config.VISIT_TO}. Другое время?")
    await state.update_data(visit_at=visit.strftime(FMT))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(n), callback_data=f"ppl:{n}") for n in (1, 2, 3, 4)
    ]])
    await state.set_state(Form.people)
    await m.answer("Сколько вас будет человек?", reply_markup=kb)


@router.callback_query(Form.people, F.data.startswith("ppl:"))
async def got_people(c: CallbackQuery, state: FSMContext):
    await state.update_data(people=int(c.data.split(":")[1]))
    d = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="send"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="abort"),
    ]])
    await state.set_state(Form.confirm)
    await c.message.edit_text(
        f"Проверь заявку:\n🕒 {fmt_dt(d['visit_at'])}\n👥 {d['people']}\n🎯 {html.escape(d['purpose'])}",
        reply_markup=kb,
    )
    await c.answer()


@router.callback_query(Form.confirm, F.data == "abort")
async def abort(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Отменено.")
    await c.answer()


@router.callback_query(Form.confirm, F.data == "send")
async def send_request(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    u = c.from_user
    cur = q(
        "INSERT INTO requests(user_id,username,full_name,purpose,visit_at,people,created_at) "
        "VALUES(?,?,?,?,?,?,?) RETURNING id",
        u.id, u.username, u.full_name, d["purpose"], d["visit_at"], d["people"],
        now().strftime("%Y-%m-%dT%H:%M:%S"),
    )
    rid = cur.fetchone()["id"]
    r = q("SELECT * FROM requests WHERE id=?", rid).fetchone()
    for aid in ADMINS:
        try:
            msg = await bot.send_message(aid, admin_text(r), reply_markup=vote_kb(rid))
            q("INSERT INTO admin_msgs VALUES(?,?,?)", rid, aid, msg.message_id)
        except Exception:
            log.exception("cannot notify admin %s", aid)
    await c.message.edit_text(f"📬 Заявка #{rid} отправлена. Сообщу, как только решат.")
    await c.answer()


# ---------------------------------------------------------------- my requests


@router.message(Command("my"))
async def my(m: Message):
    rows = q("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 5", m.from_user.id).fetchall()
    if not rows:
        return await m.answer("Заявок пока нет. /request")
    for r in rows:
        text = f"#{r['id']} · {fmt_dt(r['visit_at'])} · {STATUS_RU[r['status']]}\n{html.escape(r['purpose'])}"
        kb = None
        if r["status"] == "approved":
            text += f"\n🔑 Код: <b>{r['code']}</b>"
        if r["status"] in ("pending", "approved"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Отменить заявку", callback_data=f"cx:{r['id']}")
            ]])
        await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cx:"))
async def cancel_request(c: CallbackQuery, bot: Bot):
    rid = int(c.data.split(":")[1])
    r = q("SELECT * FROM requests WHERE id=? AND user_id=?", rid, c.from_user.id).fetchone()
    if not r or r["status"] not in ("pending", "approved"):
        return await c.answer("Уже нельзя отменить", show_alert=True)
    q("UPDATE requests SET status='cancelled' WHERE id=?", rid)
    await refresh_admin_msgs(bot, rid, close=True)
    await c.message.edit_text(f"Заявка #{rid} отменена.")
    await c.answer()


# ---------------------------------------------------------------- admin voting


async def refresh_admin_msgs(bot: Bot, rid: int, close: bool):
    r = q("SELECT * FROM requests WHERE id=?", rid).fetchone()
    for row in q("SELECT * FROM admin_msgs WHERE request_id=?", rid).fetchall():
        try:
            await bot.edit_message_text(
                admin_text(r), chat_id=row["admin_id"], message_id=row["message_id"],
                reply_markup=None if close else vote_kb(rid),
            )
        except Exception:
            pass  # текст не изменился / сообщение удалено


def new_code() -> str:
    while True:
        code = f"{random.randint(0, 9999):04d}"
        today = now().strftime("%Y-%m-%d")
        if not q("SELECT 1 FROM requests WHERE code=? AND status='approved' AND visit_at>=?", code, today).fetchone():
            return code


@router.callback_query(F.data.startswith("v:"))
async def vote(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        return await c.answer("Ты не админ", show_alert=True)
    _, rid, val = c.data.split(":")
    rid, val = int(rid), int(val)
    r = q("SELECT * FROM requests WHERE id=?", rid).fetchone()
    if not r or r["status"] != "pending":
        return await c.answer("Заявка уже закрыта", show_alert=True)
    if q("INSERT INTO votes VALUES(?,?,?) ON CONFLICT DO NOTHING", rid, c.from_user.id, val).rowcount == 0:
        return await c.answer("Ты уже голосовал", show_alert=True)

    ups = q("SELECT COUNT(*) c FROM votes WHERE request_id=? AND approve=1", rid).fetchone()["c"]
    downs = q("SELECT COUNT(*) c FROM votes WHERE request_id=? AND approve=0", rid).fetchone()["c"]
    await c.answer("Голос принят")

    if ups >= NEED:
        code = new_code()
        q("UPDATE requests SET status='approved', code=? WHERE id=?", code, rid)
        await refresh_admin_msgs(bot, rid, close=True)
        await safe_send(
            bot, r["user_id"],
            f"✅ Заявка #{rid} одобрена!\n🕒 {fmt_dt(r['visit_at'])}\n🔑 Код у двери: <b>{code}</b>",
        )
    elif downs >= MAX_REJECTS:
        q("UPDATE requests SET status='rejected' WHERE id=?", rid)
        await refresh_admin_msgs(bot, rid, close=True)
        await safe_send(bot, r["user_id"], f"❌ Заявка #{rid} отклонена. Сорян, в другой раз.")
    else:
        await refresh_admin_msgs(bot, rid, close=False)


async def safe_send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception:
        log.warning("cannot message %s", uid)


# ---------------------------------------------------------------- admin commands


def admin_only(handler):
    @functools.wraps(handler)  # aiogram смотрит на сигнатуру handler и передаёт только нужные аргументы
    async def wrapper(*a, **kw):
        if not is_admin(kw["m"].from_user.id if "m" in kw else a[0].from_user.id):
            return
        return await handler(*a, **kw)
    return wrapper


@router.message(Command("pause"))
@admin_only
async def pause(m: Message):
    q("INSERT INTO settings VALUES('accepting','0') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    await m.answer("⛔ Приём заявок остановлен (/resume чтобы включить).")


@router.message(Command("resume"))
@admin_only
async def resume(m: Message):
    q("INSERT INTO settings VALUES('accepting','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    await m.answer("✅ Приём заявок включён.")


@router.message(Command("today"))
@admin_only
async def today(m: Message):
    d = now().strftime("%Y-%m-%d")
    rows = q(
        "SELECT * FROM requests WHERE status='approved' AND visit_at LIKE ? ORDER BY visit_at", d + "%"
    ).fetchall()
    if not rows:
        return await m.answer("На сегодня одобренных визитов нет.")
    await m.answer("\n\n".join(
        f"🕒 {r['visit_at'][-5:]} · {who(r)} · {r['people']} чел.\n🎯 {html.escape(r['purpose'])}\n🔑 {r['code']}"
        for r in rows
    ))


@router.message(Command("check"))
@admin_only
async def check(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Использование: /check 1234")
    d = now().strftime("%Y-%m-%d")
    r = q("SELECT * FROM requests WHERE code=? AND status='approved' AND visit_at>=? ORDER BY visit_at LIMIT 1",
          parts[1], d).fetchone()
    if not r:
        return await m.answer("🚫 Такого действующего кода нет.")
    await m.answer(f"✅ Код верный\n{who(r)}\n🕒 {fmt_dt(r['visit_at'])}, {r['people']} чел.\n🎯 {html.escape(r['purpose'])}")


@router.message(Command("block"))
@admin_only
async def block(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Использование: /block <id пользователя> (id виден в ссылке на имя в заявке)")
    q("INSERT INTO blocked VALUES(?) ON CONFLICT DO NOTHING", int(parts[1]))
    await m.answer("🚫 Заблокирован.")


@router.message(Command("unblock"))
@admin_only
async def unblock(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Использование: /unblock <id>")
    q("DELETE FROM blocked WHERE user_id=?", int(parts[1]))
    await m.answer("Разблокирован.")


@router.message(Command("help"))
@admin_only
async def admin_help(m: Message):
    await m.answer(
        "Команды админа:\n/today — визиты на сегодня\n/check КОД — проверить код гостя\n"
        "/pause, /resume — стоп/старт приёма заявок\n/block ID, /unblock ID — чёрный список"
    )


# ---------------------------------------------------------------- background


async def housekeeping(bot: Bot):
    while True:
        try:
            n = now()
            # просрочка висящих заявок
            for r in q("SELECT * FROM requests WHERE status='pending' AND visit_at<?", n.strftime(FMT)).fetchall():
                q("UPDATE requests SET status='expired' WHERE id=?", r["id"])
                await refresh_admin_msgs(bot, r["id"], close=True)
                await safe_send(bot, r["user_id"], f"⌛ Заявка #{r['id']} не успела получить решение и просрочена.")
            # напоминание за N минут
            soon = (n + timedelta(minutes=config.REMIND_MINUTES)).strftime(FMT)
            for r in q("SELECT * FROM requests WHERE status='approved' AND reminded=0 AND visit_at<=? AND visit_at>=?",
                       soon, n.strftime(FMT)).fetchall():
                q("UPDATE requests SET reminded=1 WHERE id=?", r["id"])
                await safe_send(bot, r["user_id"], f"⏰ Через ~{config.REMIND_MINUTES} мин твой визит в 311. Код: {r['code']}")
                for aid in ADMINS:
                    await safe_send(
                        bot, aid,
                        f"⏰ Скоро гость: {r['full_name']} в {r['visit_at'][-5:]}, {r['people']} чел. — {r['purpose']}",
                    )
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(60)


async def run_webhook(bot: Bot, dp: Dispatcher):
    path = "/webhook"
    await bot.set_webhook(config.WEBHOOK_BASE_URL.rstrip("/") + path, secret_token=config.WEBHOOK_SECRET)
    app = web.Application()
    app.router.add_get("/health", lambda _: web.Response(text="ok"))  # сюда ходит пингер, чтобы сервис не засыпал
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=config.WEBHOOK_SECRET).register(app, path=path)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("webhook on port %s", config.PORT)
    await asyncio.Event().wait()


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(housekeeping(bot))
    if config.WEBHOOK_BASE_URL:
        await run_webhook(bot, dp)
    else:
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
