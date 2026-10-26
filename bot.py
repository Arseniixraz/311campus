import asyncio
import functools
import html
import logging
import random
from datetime import date, datetime, timedelta, time as dtime
from math import ceil
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import config
from db import get_setting, q, set_setting

log = logging.getLogger("block311")
router = Router()
TZ = ZoneInfo(config.TIMEZONE)
FMT = "%Y-%m-%dT%H:%M"
ADMINS = config.ADMIN_IDS
NEED = config.REQUIRED_APPROVALS
MAX_REJECTS = len(ADMINS) - NEED + 1  # столько отказов - и нужных одобрений уже не набрать

STATUS_RU = {
    "pending": "⏳ на рассмотрении",
    "approved": "✅ одобрена",
    "rejected": "❌ отклонена",
    "cancelled": "🚫 отменена",
    "expired": "⌛ просрочена",
    "done": "🚪 визит состоялся",
}

PRESETS = [
    "📚 Учёба / делаем проект",
    "🎮 Поиграть / отдохнуть",
    "🍕 Посидеть, пообщаться",
    "📦 Забрать или отдать вещь",
    "🛠 Помочь с чем-то",
]
APPROVE_REASONS = ["Ждём, заходи", "Только ненадолго", "Веди себя потише"]
REJECT_REASONS = [
    "Нас не будет дома",
    "Мы заняты / учимся",
    "Неудобное время, выбери другое",
    "Слишком много людей",
    "Причина не подходит",
    "Отдыхаем / спим",
]

BTN_REQUEST = "📝 Подать заявку"
BTN_MY = "📋 Мои заявки"
BTN_RULES = "📜 Правила"
BTN_LIST = "📥 Заявки"
BTN_TODAY = "📅 Сегодня"
BTN_CHECK = "🔑 Проверить код"
BTN_TOGGLE = "⏯ Приём заявок"
BTN_BLOCKED = "🚫 Чёрный список"


# ---------------------------------------------------------------- helpers


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def is_admin(uid: int) -> bool:
    return uid in ADMINS


def is_blocked(uid: int) -> bool:
    return bool(q("SELECT 1 FROM blocked WHERE user_id=?", uid).fetchone())


def accepting() -> bool:
    return get_setting("accepting", "1") == "1"


def admin_name(uid: int) -> str:
    return config.ADMIN_NAMES.get(uid, str(uid))


def ikb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def menu(uid: int) -> ReplyKeyboardMarkup:
    rows = [[BTN_REQUEST, BTN_MY], [BTN_RULES]]
    if is_admin(uid):
        rows += [[BTN_LIST, BTN_TODAY], [BTN_CHECK, BTN_TOGGLE], [BTN_BLOCKED]]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_only(handler):
    @functools.wraps(handler)  # aiogram смотрит на сигнатуру handler и передаёт только нужные аргументы
    async def wrapper(*a, **kw):
        if not is_admin(a[0].from_user.id):
            return
        return await handler(*a, **kw)
    return wrapper


async def safe_send(bot: Bot, uid: int, text: str, **kw):
    try:
        await bot.send_message(uid, text, **kw)
    except Exception:
        log.warning("cannot message %s", uid)


async def show(target: Message, text: str, kb=None, edit=False):
    if edit:
        try:
            return await target.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    return await target.answer(text, reply_markup=kb)


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
        line = f"{'👍' if v['approve'] else '👎'} {html.escape(admin_name(v['admin_id']))}"
        if v["reason"]:
            line += f": {html.escape(v['reason'])}"
        lines.append(line)
    tally = "\n".join(lines) or "пока никто не голосовал"
    return (
        f"📨 <b>Заявка #{r['id']}</b> — {STATUS_RU[r['status']]}\n"
        f"👤 {who(r)}\n"
        f"🕒 {fmt_dt(r['visit_at'])}, человек: {r['people']}\n"
        f"🎯 {html.escape(r['purpose'])}\n\n"
        f"Нужно {NEED} одобрения из {len(ADMINS)}.\n{tally}"
    )


def admin_kb(r) -> InlineKeyboardMarkup:
    rid, rows = r["id"], []
    if r["status"] == "pending":
        rows.append([("👍 Одобрить", f"v:{rid}:1"), ("👎 Отклонить", f"v:{rid}:0")])
    rows.append([("💬 Написать гостю", f"ch:{rid}")])
    rows.append([("🚫 Блок гостя", f"bl:{rid}"), ("🗑 Удалить", f"del:{rid}")])
    return ikb(*rows)


def get_request(rid: int):
    return q("SELECT * FROM requests WHERE id=?", rid).fetchone()


async def refresh_admin_msgs(bot: Bot, rid: int, extra: tuple | None = None):
    """Обновить сообщения с заявкой у всех админов (и, если надо, сообщение из списка)."""
    r = get_request(rid)
    if not r:
        return
    targets = [(row["admin_id"], row["message_id"]) for row in q("SELECT * FROM admin_msgs WHERE request_id=?", rid).fetchall()]
    if extra and tuple(extra) not in targets:
        targets.append(tuple(extra))
    for chat_id, message_id in targets:
        try:
            await bot.edit_message_text(admin_text(r), chat_id=chat_id, message_id=message_id, reply_markup=admin_kb(r))
        except Exception:
            pass  # текст не изменился / сообщение удалено


def guest_reasons(rid: int) -> str:
    rows = q("SELECT reason FROM votes WHERE request_id=? AND reason IS NOT NULL AND reason<>''", rid).fetchall()
    if not rows:
        return ""
    return "\n\n💬 Что написали жильцы:\n" + "\n".join(f"• {html.escape(x['reason'])}" for x in rows)


def today_text() -> str | None:
    d = now().strftime("%Y-%m-%d")
    rows = q("SELECT * FROM requests WHERE status='approved' AND visit_at LIKE ? ORDER BY visit_at", d + "%").fetchall()
    if not rows:
        return None
    return "\n\n".join(
        f"🕒 {r['visit_at'][-5:]} · {who(r)} · {r['people']} чел.\n🎯 {html.escape(r['purpose'])}\n🔑 {r['code']}"
        for r in rows
    )


def new_code() -> str:
    while True:
        code = f"{random.randint(0, 9999):04d}"
        today = now().strftime("%Y-%m-%d")
        if not q("SELECT 1 FROM requests WHERE code=? AND status='approved' AND visit_at>=?", code, today).fetchone():
            return code


# ---------------------------------------------------------------- states


class Form(StatesGroup):
    purpose = State()
    day = State()
    time = State()
    people = State()
    confirm = State()


class AdminReason(StatesGroup):
    reason = State()


class CheckCode(StatesGroup):
    code = State()


class AdminChat(StatesGroup):
    chat = State()


class GuestChat(StatesGroup):
    chat = State()


# ================================================================ 1. commands and menu buttons
# (регистрируются раньше обработчиков состояний, чтобы кнопки меню работали в любой момент)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "👋 Это бот блока 311. У нас карантин: зайти можно только по одобренной заявке.\n"
        "Пользуйся кнопками внизу 👇\n\n" + config.RULES,
        reply_markup=menu(m.from_user.id),
    )


@router.message(Command("cancel"))
async def cancel_form(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Окей, отменил.", reply_markup=menu(m.from_user.id))


@router.message(F.text == BTN_RULES)
@router.message(Command("rules"))
async def rules(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(config.RULES)


@router.message(F.text == BTN_REQUEST)
@router.message(Command("request"))
async def request_cmd(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if is_blocked(uid):
        return await m.answer("Тебе сюда нельзя 🙅")
    if not accepting():
        return await m.answer("⛔ Сейчас заявки не принимаем. Попробуй позже.")
    if q("SELECT COUNT(*) c FROM requests WHERE user_id=? AND status='pending'", uid).fetchone()["c"]:
        return await m.answer("У тебя уже есть заявка на рассмотрении. Дождись решения или отмени её в «📋 Мои заявки».")
    day_start = now().strftime("%Y-%m-%dT00:00:00")
    if q("SELECT COUNT(*) c FROM requests WHERE user_id=? AND created_at>=?", uid, day_start).fetchone()["c"] >= config.MAX_REQUESTS_PER_DAY:
        return await m.answer(f"Лимит — {config.MAX_REQUESTS_PER_DAY} заявки в день. Приходи завтра.")
    await state.set_state(Form.purpose)
    rows = [[(p, f"pp:{i}")] for i, p in enumerate(PRESETS)] + [[("✍️ Свой вариант", "pp:x")]]
    await m.answer("По какому вопросу хочешь зайти? Выбери или напиши своё:", reply_markup=ikb(*rows))


@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def my(m: Message, state: FSMContext):
    await state.clear()
    rows = q("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 5", m.from_user.id).fetchall()
    if not rows:
        return await m.answer("Заявок пока нет. Нажми «📝 Подать заявку».")
    for r in rows:
        text = f"#{r['id']} · {fmt_dt(r['visit_at'])} · {STATUS_RU[r['status']]}\n{html.escape(r['purpose'])}"
        kb = None
        if r["status"] == "approved":
            text += f"\n🔑 Код: <b>{r['code']}</b>"
        if r["status"] in ("pending", "approved"):
            kb = ikb([("Отменить заявку", f"cx:{r['id']}")])
        await m.answer(text, reply_markup=kb)


# ---- админские

@router.message(F.text == BTN_LIST)
@router.message(Command("requests"))
@admin_only
async def list_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Какие заявки показать?",
        reply_markup=ikb([("⏳ Ожидают", "ls:pending"), ("✅ Одобрены", "ls:approved")], [("📚 Последние 10", "ls:all")]),
    )


@router.message(F.text == BTN_TODAY)
@router.message(Command("today"))
@admin_only
async def today(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(today_text() or "На сегодня одобренных визитов нет.")


@router.message(F.text == BTN_CHECK)
@admin_only
async def check_button(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(CheckCode.code)
    await m.answer("Введи 4-значный код гостя:")


@router.message(Command("check"))
@admin_only
async def check_cmd(m: Message, state: FSMContext):
    await state.clear()
    parts = (m.text or "").split()
    if len(parts) < 2:
        await state.set_state(CheckCode.code)
        return await m.answer("Введи 4-значный код гостя:")
    await do_check(m, parts[1])


async def do_check(m: Message, code: str):
    d = now().strftime("%Y-%m-%d")
    r = q(
        "SELECT * FROM requests WHERE code=? AND status='approved' AND visit_at>=? ORDER BY visit_at LIMIT 1", code, d
    ).fetchone()
    if r:
        return await m.answer(
            f"✅ Код верный\n{who(r)}\n🕒 {fmt_dt(r['visit_at'])}, {r['people']} чел.\n🎯 {html.escape(r['purpose'])}",
            reply_markup=ikb([("🚪 Гость пришёл — закрыть код", f"in:{r['id']}")]),
        )
    used = q("SELECT 1 FROM requests WHERE code=? AND status='done' AND visit_at>=?", code, d).fetchone()
    await m.answer("⚠️ Этот код уже использован." if used else "🚫 Такого действующего кода нет.")


@router.message(F.text == BTN_TOGGLE)
@admin_only
async def toggle(m: Message, state: FSMContext):
    await state.clear()
    on = not accepting()
    set_setting("accepting", "1" if on else "0")
    await m.answer("✅ Приём заявок включён." if on else "⛔ Приём заявок остановлен. Нажми кнопку ещё раз, чтобы включить.")


@router.message(Command("pause"))
@admin_only
async def pause(m: Message):
    set_setting("accepting", "0")
    await m.answer("⛔ Приём заявок остановлен.")


@router.message(Command("resume"))
@admin_only
async def resume(m: Message):
    set_setting("accepting", "1")
    await m.answer("✅ Приём заявок включён.")


@router.message(F.text == BTN_BLOCKED)
@admin_only
async def blocked_list(m: Message, state: FSMContext):
    await state.clear()
    rows = q("SELECT * FROM blocked").fetchall()
    if not rows:
        return await m.answer("Чёрный список пуст. Заблокировать можно кнопкой «🚫 Блок гостя» под заявкой.")
    for b in rows:
        await m.answer(f"🚫 {html.escape(b['name'] or 'без имени')} (id {b['user_id']})", reply_markup=ikb([("✅ Разблокировать", f"ub:{b['user_id']}")]))


@router.message(Command("block"))
@admin_only
async def block_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Использование: /block <id>. Проще — кнопка «🚫 Блок гостя» под заявкой.")
    q("INSERT INTO blocked(user_id) VALUES(?) ON CONFLICT DO NOTHING", int(parts[1]))
    await m.answer("🚫 Заблокирован.")


@router.message(Command("unblock"))
@admin_only
async def unblock_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Использование: /unblock <id>")
    q("DELETE FROM blocked WHERE user_id=?", int(parts[1]))
    await m.answer("Разблокирован.")


@router.message(Command("help"))
@admin_only
async def admin_help(m: Message):
    await m.answer(
        "Всё есть в кнопках меню. Команды: /requests, /today, /check КОД, /pause, /resume, /block ID, /unblock ID."
    )


# ================================================================ 2. guest: request form


async def ask_day(target: Message, state: FSMContext, edit: bool):
    today_ = now().date()
    labels = ["Сегодня", "Завтра", (today_ + timedelta(days=2)).strftime("%d.%m")]
    await state.set_state(Form.day)
    await show(target, "Когда?", ikb([(labels[i], f"day:{i}") for i in range(3)]), edit)


@router.callback_query(Form.purpose, F.data.startswith("pp:"))
async def got_preset(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    await c.answer()
    if k == "x":
        return await c.message.edit_text("Напиши коротко, зачем хочешь зайти:")
    await state.update_data(purpose=PRESETS[int(k)][2:].strip())
    await ask_day(c.message, state, edit=True)


@router.message(Form.purpose, F.text)
async def got_purpose(m: Message, state: FSMContext):
    if len(m.text) < 3 or len(m.text) > 300:
        return await m.answer("Опиши от 3 до 300 символов.")
    await state.update_data(purpose=m.text.strip())
    await ask_day(m, state, edit=False)


def parse_hm(s: str) -> dtime:
    return datetime.strptime(s, "%H:%M").time()


def check_visit(day: date, t: dtime) -> str | None:
    if datetime.combine(day, t) < now() + timedelta(minutes=config.MIN_LEAD_MINUTES):
        return f"Заявку нужно подавать минимум за {config.MIN_LEAD_MINUTES} мин до визита. Другое время?"
    if not (parse_hm(config.VISIT_FROM) <= t <= parse_hm(config.VISIT_TO)):
        return f"Мы принимаем с {config.VISIT_FROM} до {config.VISIT_TO}. Другое время?"
    return None


def time_slots(day: date) -> list[str]:
    slots = [
        f"{h:02d}:00" for h in range(24) if not check_visit(day, dtime(h, 0))
    ]
    if len(slots) > 8:
        step = ceil(len(slots) / 8)
        slots = slots[::step]
    return slots


@router.callback_query(Form.day, F.data.startswith("day:"))
async def got_day(c: CallbackQuery, state: FSMContext):
    day = now().date() + timedelta(days=int(c.data.split(":")[1]))
    await state.update_data(day=day.isoformat())
    await state.set_state(Form.time)
    slots = time_slots(day)
    rows = [[(s, f"tm:{s}") for s in slots[i : i + 4]] for i in range(0, len(slots), 4)]
    hint = "Выбери время кнопкой или напиши своё в формате ЧЧ:ММ" if rows else "На этот день уже поздно. Напиши время или начни заново (/cancel)"
    await c.message.edit_text(f"Дата: {day.strftime('%d.%m')}. {hint} (принимаем с {config.VISIT_FROM} до {config.VISIT_TO}).", reply_markup=ikb(*rows) if rows else None)
    await c.answer()


async def accept_time(target: Message, state: FSMContext, t: dtime, edit: bool):
    d = await state.get_data()
    day = date.fromisoformat(d["day"])
    err = check_visit(day, t)
    if err:
        return await target.answer(err)
    await state.update_data(visit_at=datetime.combine(day, t).strftime(FMT))
    await state.set_state(Form.people)
    await show(target, "Сколько вас будет человек?", ikb([(str(n), f"ppl:{n}") for n in (1, 2, 3, 4)]), edit)


@router.callback_query(Form.time, F.data.startswith("tm:"))
async def got_time_chip(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await accept_time(c.message, state, parse_hm(c.data[3:]), edit=True)


@router.message(Form.time, F.text)
async def got_time(m: Message, state: FSMContext):
    try:
        t = parse_hm(m.text.strip().replace(".", ":"))
    except ValueError:
        return await m.answer("Не понял время. Пример: 17:00")
    await accept_time(m, state, t, edit=False)


@router.callback_query(Form.people, F.data.startswith("ppl:"))
async def got_people(c: CallbackQuery, state: FSMContext):
    await state.update_data(people=int(c.data.split(":")[1]))
    d = await state.get_data()
    await state.set_state(Form.confirm)
    await c.message.edit_text(
        f"Проверь заявку:\n🕒 {fmt_dt(d['visit_at'])}\n👥 {d['people']}\n🎯 {html.escape(d['purpose'])}",
        reply_markup=ikb([("✅ Отправить", "send"), ("✖️ Отмена", "abort")]),
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
    rid = q(
        "INSERT INTO requests(user_id,username,full_name,purpose,visit_at,people,created_at) "
        "VALUES(?,?,?,?,?,?,?) RETURNING id",
        u.id, u.username, u.full_name, d["purpose"], d["visit_at"], d["people"],
        now().strftime("%Y-%m-%dT%H:%M:%S"),
    ).fetchone()["id"]
    r = get_request(rid)
    for aid in ADMINS:
        try:
            msg = await bot.send_message(aid, admin_text(r), reply_markup=admin_kb(r))
            q("INSERT INTO admin_msgs VALUES(?,?,?)", rid, aid, msg.message_id)
        except Exception:
            log.exception("cannot notify admin %s", aid)
    await c.message.edit_text(f"📬 Заявка #{rid} отправлена. Сообщу, как только решат.")
    await c.answer()


@router.callback_query(F.data.startswith("cx:"))
async def cancel_request(c: CallbackQuery, bot: Bot):
    rid = int(c.data.split(":")[1])
    r = q("SELECT * FROM requests WHERE id=? AND user_id=?", rid, c.from_user.id).fetchone()
    if not r or r["status"] not in ("pending", "approved"):
        return await c.answer("Уже нельзя отменить", show_alert=True)
    q("UPDATE requests SET status='cancelled' WHERE id=?", rid)
    await refresh_admin_msgs(bot, rid)
    await c.message.edit_text(f"Заявка #{rid} отменена.")
    await c.answer()


# ================================================================ 3. admins: voting with reasons


@router.callback_query(F.data.startswith("v:"))
@admin_only
async def vote_click(c: CallbackQuery, state: FSMContext):
    _, rid, val = c.data.split(":")
    rid, val = int(rid), int(val)
    r = get_request(rid)
    if not r or r["status"] != "pending":
        return await c.answer("Заявка уже закрыта", show_alert=True)
    if q("SELECT 1 FROM votes WHERE request_id=? AND admin_id=?", rid, c.from_user.id).fetchone():
        return await c.answer("Ты уже голосовал", show_alert=True)
    await state.clear()
    await state.set_state(AdminReason.reason)
    await state.update_data(rid=rid, val=val, origin=[c.message.chat.id, c.message.message_id])
    options = APPROVE_REASONS if val else REJECT_REASONS
    rows = [[(o, f"rs:{i}")] for i, o in enumerate(options)] + [[("Без причины", "rs:-")]]
    await c.message.answer(
        f"{'👍 Одобряешь' if val else '👎 Отклоняешь'} заявку #{rid}. Напиши причину или выбери из списка — "
        "гость увидит её (без твоего имени). Голос засчитается после этого шага.",
        reply_markup=ikb(*rows),
    )
    await c.answer()


async def finish_vote(bot: Bot, admin_id: int, d: dict, reason: str | None) -> str:
    rid, val = d["rid"], d["val"]
    r = get_request(rid)
    if not r or r["status"] != "pending":
        return "Заявка уже закрыта."
    if q(
        "INSERT INTO votes(request_id,admin_id,approve,reason) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
        rid, admin_id, val, reason,
    ).rowcount == 0:
        return "Ты уже голосовал."
    ups = q("SELECT COUNT(*) c FROM votes WHERE request_id=? AND approve=1", rid).fetchone()["c"]
    downs = q("SELECT COUNT(*) c FROM votes WHERE request_id=? AND approve=0", rid).fetchone()["c"]
    if ups >= NEED:
        code = new_code()
        q("UPDATE requests SET status='approved', code=? WHERE id=?", code, rid)
        await safe_send(
            bot, r["user_id"],
            f"✅ Заявка #{rid} одобрена!\n🕒 {fmt_dt(r['visit_at'])}\n🔑 Код у двери: <b>{code}</b>" + guest_reasons(rid),
        )
    elif downs >= MAX_REJECTS:
        q("UPDATE requests SET status='rejected' WHERE id=?", rid)
        await safe_send(bot, r["user_id"], f"❌ Заявка #{rid} отклонена." + guest_reasons(rid))
    await refresh_admin_msgs(bot, rid, extra=d.get("origin"))
    return "Голос принят ✅"


@router.callback_query(AdminReason.reason, F.data.startswith("rs:"))
async def reason_pick(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    k = c.data[3:]
    reason = None if k == "-" else (APPROVE_REASONS if d["val"] else REJECT_REASONS)[int(k)]
    result = await finish_vote(bot, c.from_user.id, d, reason)
    await c.message.edit_text(result)
    await c.answer()


@router.message(AdminReason.reason, F.text)
async def reason_text(m: Message, state: FSMContext, bot: Bot):
    if len(m.text) > 200:
        return await m.answer("Покороче, до 200 символов.")
    d = await state.get_data()
    await state.clear()
    await m.answer(await finish_vote(bot, m.from_user.id, d, m.text.strip()), reply_markup=menu(m.from_user.id))


# ================================================================ 4. admins: list, delete, block, check-in


@router.callback_query(F.data.startswith("ls:"))
@admin_only
async def list_show(c: CallbackQuery):
    kind = c.data[3:]
    sql = {
        "pending": "SELECT * FROM requests WHERE status='pending' ORDER BY id LIMIT 10",
        "approved": "SELECT * FROM requests WHERE status='approved' ORDER BY visit_at LIMIT 10",
        "all": "SELECT * FROM requests ORDER BY id DESC LIMIT 10",
    }[kind]
    rows = q(sql).fetchall()
    await c.answer()
    if not rows:
        return await c.message.answer("Тут пусто.")
    for r in rows:
        await c.message.answer(admin_text(r), reply_markup=admin_kb(r))


@router.callback_query(F.data.startswith("del:"))
@admin_only
async def delete_ask(c: CallbackQuery):
    rid = int(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=ikb([("🗑 Да, удалить", f"dly:{rid}"), ("Отмена", f"dln:{rid}")]))
    await c.answer("Удалить заявку насовсем?")


@router.callback_query(F.data.startswith("dln:"))
@admin_only
async def delete_no(c: CallbackQuery):
    r = get_request(int(c.data.split(":")[1]))
    if r:
        await c.message.edit_reply_markup(reply_markup=admin_kb(r))
    await c.answer("Не удаляю")


@router.callback_query(F.data.startswith("dly:"))
@admin_only
async def delete_yes(c: CallbackQuery, bot: Bot):
    rid = int(c.data.split(":")[1])
    r = get_request(rid)
    if not r:
        return await c.answer("Уже удалена", show_alert=True)
    targets = [(x["admin_id"], x["message_id"]) for x in q("SELECT * FROM admin_msgs WHERE request_id=?", rid).fetchall()]
    q("DELETE FROM votes WHERE request_id=?", rid)
    q("DELETE FROM admin_msgs WHERE request_id=?", rid)
    q("DELETE FROM chat_admins WHERE request_id=?", rid)
    q("DELETE FROM requests WHERE id=?", rid)
    text = f"🗑 Заявка #{rid} удалена ({html.escape(admin_name(c.from_user.id))})"
    for chat_id, message_id in targets + [(c.message.chat.id, c.message.message_id)]:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    if r["status"] in ("pending", "approved"):
        await safe_send(bot, r["user_id"], f"🗑 Заявка #{rid} отменена жильцами.")
    await c.answer("Удалено")


@router.callback_query(F.data.startswith("bl:"))
@admin_only
async def block_guest(c: CallbackQuery):
    r = get_request(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Заявка не найдена", show_alert=True)
    q("INSERT INTO blocked(user_id,name) VALUES(?,?) ON CONFLICT DO NOTHING", r["user_id"], r["full_name"])
    await c.answer(f"{r['full_name']} в чёрном списке", show_alert=True)


@router.callback_query(F.data.startswith("ub:"))
@admin_only
async def unblock_guest(c: CallbackQuery):
    q("DELETE FROM blocked WHERE user_id=?", int(c.data.split(":")[1]))
    await c.message.edit_text("✅ Разблокирован.")
    await c.answer()


@router.callback_query(F.data.startswith("in:"))
@admin_only
async def check_in(c: CallbackQuery, bot: Bot):
    rid = int(c.data.split(":")[1])
    if q("UPDATE requests SET status='done' WHERE id=? AND status='approved'", rid).rowcount == 0:
        return await c.answer("Код уже закрыт", show_alert=True)
    await refresh_admin_msgs(bot, rid)
    await c.message.edit_text(f"🚪 Визит по заявке #{rid} отмечен ({html.escape(admin_name(c.from_user.id))}). Код больше не действует.")
    await c.answer()


# ================================================================ 5. state handlers with free text


@router.message(CheckCode.code, F.text)
@admin_only
async def check_code_text(m: Message, state: FSMContext):
    await state.clear()
    await do_check(m, m.text.strip())


# ---- чат админ <-> гость через бота (имя админа гость не видит)


@router.callback_query(F.data.startswith("ch:"))
@admin_only
async def chat_open(c: CallbackQuery, state: FSMContext):
    rid = int(c.data.split(":")[1])
    r = get_request(rid)
    if not r:
        return await c.answer("Заявка удалена", show_alert=True)
    await state.clear()
    await state.set_state(AdminChat.chat)
    await state.update_data(rid=rid)
    await c.message.answer(
        f"💬 Чат с гостем по заявке #{rid} ({who(r)}). Всё, что напишешь, уйдёт ему от имени жильцов — "
        "твоё имя он не увидит. Остальные админы увидят копию.\nЗавершить — кнопка ниже или любая кнопка меню.",
        reply_markup=ikb([("Завершить чат", "chx")]),
    )
    await c.answer()


@router.callback_query(F.data == "chx")
async def chat_close(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Чат завершён.")
    await c.answer()


@router.message(AdminChat.chat, F.text)
@admin_only
async def admin_to_guest(m: Message, state: FSMContext, bot: Bot):
    rid = (await state.get_data())["rid"]
    r = get_request(rid)
    if not r:
        await state.clear()
        return await m.answer("Заявка уже удалена, чат закрыт.")
    if len(m.text) > 1000:
        return await m.answer("Слишком длинно, до 1000 символов.")
    try:
        await bot.send_message(
            r["user_id"],
            f"💬 <b>Жильцы 311 (заявка #{rid}):</b>\n{html.escape(m.text)}",
            reply_markup=ikb([("✏️ Ответить", f"gr:{rid}")]),
        )
    except Exception:
        return await m.answer("Не доставилось: гость мог заблокировать бота.")
    q("INSERT INTO chat_admins VALUES(?,?) ON CONFLICT DO NOTHING", rid, m.from_user.id)
    await m.answer("✅ Отправлено")
    for aid in ADMINS:
        if aid != m.from_user.id:
            await safe_send(
                bot, aid, f"💬 {html.escape(admin_name(m.from_user.id))} → гостю по #{rid}:\n{html.escape(m.text)}"
            )


@router.callback_query(F.data.startswith("gr:"))
async def guest_reply_open(c: CallbackQuery, state: FSMContext):
    rid = int(c.data.split(":")[1])
    r = get_request(rid)
    if not r or r["user_id"] != c.from_user.id:
        return await c.answer("Этот чат уже закрыт", show_alert=True)
    if is_blocked(c.from_user.id):
        return await c.answer("Тебе сюда нельзя", show_alert=True)
    await state.clear()
    await state.set_state(GuestChat.chat)
    await state.update_data(rid=rid)
    await c.message.answer("Пиши ответ — я передам жильцам. Выйти: /cancel или кнопка меню.")
    await c.answer()


@router.message(GuestChat.chat, F.text)
async def guest_to_admins(m: Message, state: FSMContext, bot: Bot):
    uid, day = m.from_user.id, now().strftime("%Y-%m-%d")
    r = get_request((await state.get_data())["rid"])
    if not r or r["user_id"] != uid or is_blocked(uid):
        await state.clear()
        return await m.answer("Этот чат уже закрыт.")
    if len(m.text) > 500:
        return await m.answer("Слишком длинно, до 500 символов.")
    used = q("SELECT n FROM relay_usage WHERE user_id=? AND day=?", uid, day).fetchone()
    if used and used["n"] >= config.RELAY_DAILY_LIMIT:
        return await m.answer("На сегодня лимит сообщений жильцам исчерпан. Попробуй завтра.")
    q("INSERT INTO relay_usage(user_id,day,n) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET n=relay_usage.n+1", uid, day)
    talked = [x["admin_id"] for x in q("SELECT admin_id FROM chat_admins WHERE request_id=?", r["id"]).fetchall()]
    for aid in ADMINS:  # те, кто уже писал гостю, получают кнопку «Ответить»; остальные — копию
        await safe_send(
            bot, aid, f"💬 <b>{who(r)}</b> по заявке #{r['id']}:\n{html.escape(m.text)}",
            reply_markup=ikb([("💬 Ответить", f"ch:{r['id']}")]) if aid in talked or not talked else None,
        )
    await m.answer("✅ Передал жильцам.")


# ================================================================ 6. fallbacks


@router.message(F.text)
async def unknown_text(m: Message, state: FSMContext):
    if await state.get_state():
        return await m.answer("Ответь кнопкой выше или нажми /cancel, чтобы выйти.")
    await m.answer("Не понял 🤔 Пользуйся кнопками внизу.", reply_markup=menu(m.from_user.id))


@router.callback_query()
async def stale_button(c: CallbackQuery):
    await c.answer("Эта кнопка устарела", show_alert=True)


@router.errors()
async def on_error(event: ErrorEvent):
    log.error("handler failed", exc_info=event.exception)
    src = event.update.callback_query or event.update.message
    if not src:
        return
    text = "⚠️ Что-то пошло не так, попробуй ещё раз."
    if is_admin(src.from_user.id):  # админу показываем причину, чтобы не лезть в логи
        text = f"⚠️ Ошибка: {type(event.exception).__name__}: {str(event.exception)[:200]}"
    try:
        if event.update.callback_query:
            await src.answer(text, show_alert=True)
        else:
            await src.answer(text)
    except Exception:
        pass


# ---------------------------------------------------------------- background


async def housekeeping(bot: Bot):
    while True:
        try:
            n = now()
            # просрочка висящих заявок
            for r in q("SELECT * FROM requests WHERE status='pending' AND visit_at<?", n.strftime(FMT)).fetchall():
                q("UPDATE requests SET status='expired' WHERE id=?", r["id"])
                await refresh_admin_msgs(bot, r["id"])
                await safe_send(bot, r["user_id"], f"⌛ Заявка #{r['id']} не успела получить решение и просрочена.")
            # напоминание за N минут
            soon = (n + timedelta(minutes=config.REMIND_MINUTES)).strftime(FMT)
            for r in q(
                "SELECT * FROM requests WHERE status='approved' AND reminded=0 AND visit_at<=? AND visit_at>=?",
                soon, n.strftime(FMT),
            ).fetchall():
                q("UPDATE requests SET reminded=1 WHERE id=?", r["id"])
                await safe_send(bot, r["user_id"], f"⏰ Через ~{config.REMIND_MINUTES} мин твой визит в 311. Код: {r['code']}")
                for aid in ADMINS:
                    await safe_send(
                        bot, aid,
                        f"⏰ Скоро гость: {html.escape(r['full_name'] or '?')} в {r['visit_at'][-5:]}, "
                        f"{r['people']} чел. — {html.escape(r['purpose'])}",
                    )
            # утренняя сводка админам
            today_s = n.strftime("%Y-%m-%d")
            if n.hour >= 9 and get_setting("digest_date") != today_s:
                set_setting("digest_date", today_s)
                text = today_text()
                if text:
                    for aid in ADMINS:
                        await safe_send(bot, aid, "☀️ Гости на сегодня:\n\n" + text)
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
