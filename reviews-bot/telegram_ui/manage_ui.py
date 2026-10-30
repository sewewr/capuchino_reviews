"""
Третий этап в Telegram: сравнение филиалов, динамика рейтингов,
ежемесячный управленческий отчёт.

/branches  — сравнение филиалов за неделю, месяц или прошлый месяц
/dynamics  — график средней оценки и числа отзывов по неделям или месяцам
/monthly   — управленческий отчёт за прошлый месяц (1-го числа приходит сам)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message)

import analytics
import management
import storage

log = logging.getLogger(__name__)

router = Router(name="manage")


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


# --- Сравнение филиалов -------------------------------------------------------

BRANCH_PERIODS = {
    "week": lambda: analytics.period_week(),
    "month": lambda: analytics.period_month(),
    "lastmonth": lambda: management.last_full_month(),
}


def branches_keyboard(active: str) -> InlineKeyboardMarkup:
    labels = {"week": "Неделя", "month": "30 дней", "lastmonth": "Прошлый месяц"}
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn(("• " if k == active else "") + v, f"br:{k}") for k, v in labels.items()]])


def branches_text(conn, key: str) -> str:
    period = BRANCH_PERIODS[key]()
    prev = management.previous_month(period) if key == "lastmonth" else None
    return management.render_comparison(management.compare_branches(conn, period, prev))


@router.message(Command("branches"))
async def cmd_branches(message: Message, conn) -> None:
    await message.answer(branches_text(conn, "month"), reply_markup=branches_keyboard("month"))


@router.callback_query(F.data.startswith("br:"))
async def on_branches(callback: CallbackQuery, conn) -> None:
    key = callback.data.split(":", 1)[1]
    await callback.answer()
    if key not in BRANCH_PERIODS:
        return
    try:
        await callback.message.edit_text(branches_text(conn, key), reply_markup=branches_keyboard(key))
    except Exception as e:
        log.debug("сравнение не обновилось: %s", e)


# --- Динамика -------------------------------------------------------------------


def dynamics_keyboard(by: str, branch: str) -> InlineKeyboardMarkup:
    def b(label, k):
        return _btn(("• " if k == by else "") + label, f"dy:{k}:{branch}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [b("По неделям", "week"), b("По месяцам", "month")],
        [_btn("🏠 Выбрать филиал ▾" if not branch else "🏠 Вся сеть", f"dp:{by}" if not branch else f"dy:{by}:")],
    ])


def dynamics_branch_keyboard(by: str, conn) -> InlineKeyboardMarkup:
    import search

    buttons = [_btn(name, f"dy:{by}:{key}") for key, name in search.branch_choices(conn)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_btn("Вся сеть", f"dy:{by}:")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_dynamics(conn, by: str, branch: str):
    series = management.dynamics(conn, by, branch or None)
    caption = management.describe_trend(series, by)
    png = management.chart_png(series, by) if sum(p.total for p in series.points) else None
    return png, caption


@router.message(Command("dynamics"))
async def cmd_dynamics(message: Message, conn) -> None:
    png, caption = await asyncio.to_thread(_render_dynamics, conn, "week", "")
    kb = dynamics_keyboard("week", "")
    if png:
        await message.answer_photo(BufferedInputFile(png, "dynamics.png"), caption=caption,
                                   reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)


@router.callback_query(F.data.startswith("dp:"))
async def on_dynamics_picker(callback: CallbackQuery, conn) -> None:
    by = callback.data.split(":", 1)[1]
    await callback.answer("Выберите филиал")
    try:
        await callback.message.edit_reply_markup(reply_markup=dynamics_branch_keyboard(by, conn))
    except Exception as e:
        log.debug("выбор филиала не открылся: %s", e)


@router.callback_query(F.data.startswith("dy:"))
async def on_dynamics(callback: CallbackQuery, conn) -> None:
    try:
        _, by, branch = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    if by not in ("week", "month") or (branch and not branch.isdigit()):
        await callback.answer()
        return
    await callback.answer()
    png, caption = await asyncio.to_thread(_render_dynamics, conn, by, branch)
    kb = dynamics_keyboard(by, branch)
    msg = callback.message
    try:
        if png and msg.photo:
            await msg.edit_media(InputMediaPhoto(media=BufferedInputFile(png, "dynamics.png"),
                                                 caption=caption), reply_markup=kb)
        elif png:
            await msg.answer_photo(BufferedInputFile(png, "dynamics.png"), caption=caption,
                                   reply_markup=kb)
        elif msg.photo:
            await msg.answer(caption, reply_markup=kb)
        else:
            await msg.edit_text(caption, reply_markup=kb)
    except Exception as e:
        log.debug("динамика не обновилась: %s", e)


# --- Ежемесячный отчёт ----------------------------------------------------------


async def send_monthly(bot: Bot, chat_id, conn, period, now: datetime | None = None) -> None:
    from telegram_ui.stats_ui import catch_up_topics

    await catch_up_topics(conn, max_batches=20)
    report = await asyncio.to_thread(management.build_monthly, conn, period, now)
    await bot.send_message(chat_id, report.text)
    await bot.send_message(chat_id, report.comparison)
    await bot.send_message(chat_id, report.topics_text)
    if report.chart:
        await bot.send_photo(chat_id, BufferedInputFile(report.chart, "months.png"),
                             caption=report.chart_caption)


@router.message(Command("monthly"))
async def cmd_monthly(message: Message, conn, command: CommandObject | None = None) -> None:
    arg = ((command.args if command else None) or "").strip().lower()
    period = (management.this_month_so_far() if arg in ("сейчас", "текущий", "now")
              else management.last_full_month())
    wait = await message.answer(f"Собираю отчёт: {period.title.lower()}…")
    await send_monthly(message.bot, message.chat.id, conn, period)
    try:
        await wait.delete()
    except Exception:
        pass


def monthly_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("09:00", "mm:09:00"), _btn("10:00", "mm:10:00"), _btn("12:00", "mm:12:00")],
        [_btn("🔕 Выключить" if enabled else "🔔 Включить", "mm:off" if enabled else "mm:on")],
        [_btn("← Назад", "mn:settings")],
    ])


def monthly_settings_text(conn) -> str:
    enabled, at = management.monthly_schedule(conn)
    state = (f"приходит 1-го числа в {at:%H:%M} (МСК) в чат еженедельного отчёта"
             if enabled else "выключен")
    return (f"📅 <b>Ежемесячный управленческий отчёт</b>\nСейчас: {state}.\n\n"
            "В отчёте: итоги месяца и сравнение с прошлым, рейтинг филиалов, причины "
            "негатива и что хвалят, график динамики и выводы ИИ.")


@router.callback_query(F.data.startswith("mm:"))
async def on_monthly_settings(callback: CallbackQuery, conn) -> None:
    raw = callback.data.split(":", 1)[1]
    if raw in ("on", "off"):
        storage.set_state(conn, management.MONTHLY_ENABLED, "1" if raw == "on" else "0")
    else:
        storage.set_state(conn, management.MONTHLY_TIME, raw)
        storage.set_state(conn, management.MONTHLY_ENABLED, "1")
    enabled, _ = management.monthly_schedule(conn)
    await callback.answer("Готово")
    try:
        await callback.message.edit_text(monthly_settings_text(conn),
                                         reply_markup=monthly_keyboard(enabled))
    except Exception as e:
        log.debug("настройки отчёта не обновились: %s", e)


async def monthly_if_due(bot: Bot, conn, notify_admin=None, now: datetime | None = None) -> bool:
    from telegram_ui.stats_ui import default_report_chat

    now = now or datetime.now(analytics.TZ)
    enabled, at = management.monthly_schedule(conn)
    chat = analytics.load_schedule(conn, default_report_chat()).chat
    last = storage.get_state(conn, management.MONTHLY_LAST)
    if not management.monthly_due(enabled, at, chat, last, now):
        return False
    storage.set_state(conn, management.MONTHLY_LAST, management.month_key(now))
    try:
        await send_monthly(bot, chat, conn, management.last_full_month(now), now)
        log.info("ежемесячный отчёт отправлен в %s", chat)
        return True
    except Exception as e:
        log.exception("не отправил ежемесячный отчёт")
        if notify_admin:
            await notify_admin(f"Не отправился ежемесячный отчёт в чат {chat}: {e}. "
                               "Прислать вручную: /monthly")
        return False
