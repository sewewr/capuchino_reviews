"""
Аналитика в Telegram (п. 12 ТЗ) и настройка еженедельного отчёта (п. 13, 17 ТЗ).

/stats              — статистика с кнопками: сегодня, неделя, месяц, свой период
/stats 01.10 15.10  — сразу за свой период
/report             — прислать еженедельный отчёт прямо сейчас
/report_time        — когда приходит отчёт; /report_time пт 18:00, /report_time выкл
/report_here        — присылать отчёт в этот чат
/topics             — что хвалят и ругают (ИИ-разбор тем, п. 14 ТЗ)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import analytics
import storage

log = logging.getLogger(__name__)

router = Router(name="stats")

PERIODS = {
    "today": analytics.period_today,
    "week": analytics.period_week,
    "month": analytics.period_month,
}


class CustomPeriod(StatesGroup):
    waiting = State()


def stats_keyboard(active: str | None = None) -> InlineKeyboardMarkup:
    def btn(label, key):
        mark = "• " if key == active else ""
        return InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"st:{key}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Сегодня", "today"), btn("Неделя", "week"), btn("Месяц", "month")],
        [btn("📅 Свой период", "custom")],
    ])


def default_report_chat() -> str | None:
    return (os.environ.get("REVIEWS_CHAT_ID") or os.environ.get("TELEGRAM_ADMIN_CHAT")
            or None)


# --- /stats ------------------------------------------------------------------


@router.message(Command("stats"))
async def cmd_stats(message: Message, conn, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if arg:
        try:
            period = analytics.parse_period(arg)
        except ValueError as e:
            await message.answer(f"{e}.\nПример: <code>/stats 01.10 15.10</code>")
            return
        await message.answer(analytics.build_stats(conn, period),
                             reply_markup=stats_keyboard())
        return

    await message.answer(analytics.build_stats(conn, analytics.period_week()),
                         reply_markup=stats_keyboard("week"))


@router.callback_query(F.data.startswith("st:"))
async def on_stats_button(callback: CallbackQuery, state: FSMContext, conn) -> None:
    key = callback.data.split(":", 1)[1]

    if key == "custom":
        await state.set_state(CustomPeriod.waiting)
        await callback.answer()
        await callback.message.answer(
            "Пришлите даты периода, например <code>01.10 15.10</code> "
            "или <code>01.09.2026 30.09.2026</code>. Одна дата — один день.\n"
            "Передумали — /cancel"
        )
        return

    make = PERIODS.get(key)
    if make is None:
        await callback.answer()
        return
    text = analytics.build_stats(conn, make())
    try:
        await callback.message.edit_text(text, reply_markup=stats_keyboard(key))
    except Exception as e:  # «сообщение не изменилось» — не страшно
        log.debug("статистика не обновилась: %s", e)
    await callback.answer()


@router.message(Command("cancel"), CustomPeriod.waiting)
async def cancel_custom(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, отменил.")


@router.message(CustomPeriod.waiting, F.text & ~F.text.startswith("/"))
async def receive_period(message: Message, state: FSMContext, conn) -> None:
    try:
        period = analytics.parse_period(message.text)
    except ValueError as e:
        await message.answer(f"{e}. Попробуйте ещё раз или /cancel")
        return
    await state.clear()
    await message.answer(analytics.build_stats(conn, period), reply_markup=stats_keyboard())


# --- Еженедельный отчёт -------------------------------------------------------


@router.message(Command("report"))
async def cmd_report(message: Message, conn) -> None:
    now = datetime.now(analytics.TZ)
    text = analytics.build_report(conn, now=now, chat=str(message.chat.id))
    await message.answer(text, disable_web_page_preview=True)
    extra = await weekly_topics(conn, now)
    if extra:
        await message.answer(extra)


@router.message(Command("report_time"))
async def cmd_report_time(message: Message, conn, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if arg:
        try:
            day, at, enabled = analytics.parse_schedule(arg)
        except ValueError as e:
            await message.answer(f"{e}.\nПримеры: <code>/report_time пн 10:00</code>, "
                                 "<code>/report_time выкл</code>")
            return
        if day is not None:
            storage.set_state(conn, analytics.REPORT_DAY, str(day))
        if at is not None:
            storage.set_state(conn, analytics.REPORT_TIME, at.strftime("%H:%M"))
        if enabled is not None:
            storage.set_state(conn, analytics.REPORT_ENABLED, "1" if enabled else "0")

    schedule = analytics.load_schedule(conn, default_report_chat())
    where = ("в этот чат" if schedule.chat == str(message.chat.id)
             else f"в чат <code>{schedule.chat}</code>" if schedule.chat
             else "но чат не задан — наберите /report_here в нужном чате")
    head = "Готово: " if arg else "Сейчас: "
    text = f"{head}{schedule.describe()}"
    if schedule.enabled:
        text += f", {where}"
    text += ("\n\nИзменить: <code>/report_time пн 10:00</code>\n"
             "Выключить: <code>/report_time выкл</code>, включить: <code>/report_time вкл</code>\n"
             "Прислать сейчас: /report")
    await message.answer(text)


@router.message(Command("report_here"))
async def cmd_report_here(message: Message, conn) -> None:
    storage.set_state(conn, analytics.REPORT_CHAT, str(message.chat.id))
    schedule = analytics.load_schedule(conn)
    await message.answer(f"Еженедельный отчёт будет приходить сюда — {schedule.describe()}.")


# --- Темы отзывов (п. 14 ТЗ) ------------------------------------------------

TOPIC_PERIODS = {"week": analytics.period_week, "month": analytics.period_month}


def topics_keyboard(active: str) -> InlineKeyboardMarkup:
    def btn(label, key):
        return InlineKeyboardButton(text=("• " if key == active else "") + label,
                                    callback_data=f"tp:{key}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Неделя", "week"), btn("Месяц", "month")],
        [InlineKeyboardButton(text="🧠 Резюме от ИИ", callback_data=f"tp:sum:{active}")],
    ])


async def catch_up_topics(conn, max_batches: int = 3) -> str | None:
    """Разобрать свежие отзывы перед показом. Возвращает текст ошибки, если не вышло."""
    import drafts
    import topics

    if not drafts.enabled():
        return "ИИ не подключён (нет GIGACHAT_CREDENTIALS) — темы не разбираются"
    try:
        await asyncio.to_thread(topics.process_pending, conn, None, max_batches)
    except drafts.DraftError as e:
        return str(e)
    return None


def topics_text(conn, key: str, problem: str | None = None) -> str:
    import topics

    period = TOPIC_PERIODS[key]()
    text = topics.render(topics.collect(conn, period), topics.collect(conn, period.previous()))
    if problem:
        text += f"\n\n⚠️ {html.escape(problem)}"
    return text


@router.message(Command("topics"))
async def cmd_topics(message: Message, conn) -> None:
    wait = await message.answer("Разбираю свежие отзывы…")
    problem = await catch_up_topics(conn)
    await wait.edit_text(topics_text(conn, "week", problem), reply_markup=topics_keyboard("week"))


@router.callback_query(F.data.startswith("tp:"))
async def on_topics_button(callback: CallbackQuery, conn) -> None:
    import drafts
    import topics

    parts = callback.data.split(":")
    if parts[1] == "sum":
        key = parts[2] if len(parts) > 2 and parts[2] in TOPIC_PERIODS else "week"
        await callback.answer("Готовлю резюме, это займёт несколько секунд")
        period = TOPIC_PERIODS[key]()
        try:
            await catch_up_topics(conn)
            text = await asyncio.to_thread(topics.summarize, conn, period)
        except drafts.DraftError as e:
            await callback.message.answer(f"Не получилось подготовить резюме: {html.escape(str(e))}")
            return
        await callback.message.answer(
            f"🧠 <b>Резюме — {period.title.lower()}: {period.label()}</b>\n\n{html.escape(text)}")
        return

    key = parts[1] if parts[1] in TOPIC_PERIODS else "week"
    await callback.answer()
    try:
        await callback.message.edit_text(topics_text(conn, key), reply_markup=topics_keyboard(key))
    except Exception as e:
        log.debug("темы не обновились: %s", e)


async def weekly_topics(conn, now: datetime) -> str:
    """Блок «Темы недели» с резюме от ИИ для еженедельного отчёта. Пусто — если ИИ нет."""
    import drafts
    import topics

    if not drafts.enabled():
        return ""
    await catch_up_topics(conn, max_batches=20)
    period = analytics.period_report(now)
    st = topics.collect(conn, period)
    summary = None
    try:
        summary = await asyncio.to_thread(topics.summarize, conn, period)
    except Exception as e:  # отчёт уйдёт и без резюме
        log.warning("резюме недели не получилось: %s", e)
    return topics.report_block(st, summary)


# --- Фоновая отправка -------------------------------------------------------

TOPICS_EVERY = 10  # минут между фоновыми разборами тем


async def report_loop(bot: Bot, conn, notify_admin=None, now=None) -> None:
    """
    Раз в минуту проверяем, не пора ли отправить отчёт. Раз в TOPICS_EVERY
    минут разбираем новые отзывы на темы — понемногу, чтобы к отчёту всё
    было готово и /topics открывался быстро.
    """
    import drafts
    import topics

    tick = 0
    while True:
        try:
            await send_if_due(bot, conn, notify_admin)
        except Exception:
            log.exception("сбой в расписании отчёта")

        if tick % TOPICS_EVERY == 0 and drafts.enabled():
            try:
                done = await asyncio.to_thread(topics.process_pending, conn)
                if done:
                    log.info("разобрано на темы отзывов: %s", done)
            except Exception as e:
                log.warning("разбор тем отложен: %s", e)
                conn.rollback()
        tick += 1
        await asyncio.sleep(60)


async def send_if_due(bot: Bot, conn, notify_admin=None, now: datetime | None = None) -> bool:
    now = now or datetime.now(analytics.TZ)
    schedule = analytics.load_schedule(conn, default_report_chat())
    last = storage.get_state(conn, analytics.REPORT_LAST)
    if not analytics.report_due(schedule, last, now):
        return False

    # Отмечаем до отправки: если Telegram недоступен, лучше пропустить
    # одну неделю, чем слать отчёт каждую минуту.
    storage.set_state(conn, analytics.REPORT_LAST, analytics.week_key(now))
    text = analytics.build_report(conn, now=now, chat=schedule.chat)
    extra = await weekly_topics(conn, now)
    try:
        await bot.send_message(schedule.chat, text, disable_web_page_preview=True)
        if extra:  # отдельным сообщением: вместе с отчётом может не влезть в лимит Telegram
            await bot.send_message(schedule.chat, extra)
        log.info("еженедельный отчёт отправлен в %s", schedule.chat)
        return True
    except Exception as e:
        log.exception("не отправил еженедельный отчёт")
        if notify_admin:
            await notify_admin(f"Не отправился еженедельный отчёт в чат {schedule.chat}: {e}. "
                               "Прислать вручную: /report")
        return False
