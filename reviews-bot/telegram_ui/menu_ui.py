"""
Меню с кнопками: чтобы не запоминать команды.

/menu (и /start) — разделы: 📥 Отзывы, 📊 Аналитика, ⚙️ Настройки, ❓ Помощь.
Раздел открывается на месте того же сообщения, «← Назад» возвращает к
разделам. Каждая кнопка делает ровно то же, что соответствующая команда,
— команды продолжают работать для тех, кто к ним привык.

Отдельно бот регистрирует список команд в Telegram (кнопка «Меню» у поля
ввода) — в том же порядке, по разделам.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats,
                           BotCommandScopeAllPrivateChats, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

import analytics
import storage

log = logging.getLogger(__name__)

router = Router(name="menu")

NO_ARGS = SimpleNamespace(args=None)

# раздел → (заголовок, пояснение, [(кнопка, действие)])
SECTIONS = {
    "reviews": ("📥 Отзывы", "Работа с отзывами: что ждёт ответа, поиск и списки.", [
        ("🆕 Новые — никто не взял", "new"),
        ("🚨 Негатив без реакции", "negative"),
        ("🔎 Все отзывы с фильтрами", "reviews"),
        ("🔍 Поиск по тексту или имени", "find"),
        ("🚫 Скрытая реклама", "spam"),
    ]),
    "analytics": ("📊 Аналитика", "Цифры, темы, сравнение филиалов и отчёты.", [
        ("📊 Статистика", "stats"),
        ("🧭 Что хвалят и ругают", "topics"),
        ("🏆 Сравнение филиалов", "branches"),
        ("📈 Динамика рейтинга", "dynamics"),
        ("🗓 Еженедельный отчёт сейчас", "report"),
        ("📑 Управленческий отчёт за прошлый месяц", "monthly"),
        ("📑 Текущий месяц по сегодня", "monthly_now"),
    ]),
    "settings": ("⚙️ Настройки", "Порог негатива, расписание отчёта, Google Таблица.", [
        ("🎚 Порог негатива", "threshold"),
        ("⏰ Расписание еженедельного отчёта", "report_time"),
        ("📅 Ежемесячный отчёт", "monthly_settings"),
        ("📍 Присылать отчёт в этот чат", "report_here"),
        ("📤 Выгрузить в Google Таблицу", "export"),
        ("🆔 ID этого чата", "id"),
    ]),
    "help": ("❓ Помощь", "Как устроена работа с отзывом и учебные отзывы.", [
        ("📖 Как работать с отзывом", "guide"),
        ("⌨️ Все команды", "commands"),
        ("🎓 Учебные отзывы", "demo"),
        ("🧹 Убрать учебные отзывы", "clear_demo"),
    ]),
}

# Список команд для кнопки «Меню» Telegram — по разделам, по порядку
COMMANDS = [
    ("menu", "📋 Меню с кнопками"),
    ("new", "Отзывы: новые, никто не взял"),
    ("negative", "Отзывы: негатив без реакции"),
    ("reviews", "Отзывы: список с фильтрами"),
    ("find", "Отзывы: поиск по тексту или имени"),
    ("spam", "Отзывы: скрытая реклама"),
    ("stats", "Аналитика: статистика"),
    ("topics", "Аналитика: что хвалят и ругают"),
    ("branches", "Аналитика: сравнение филиалов"),
    ("dynamics", "Аналитика: динамика рейтинга"),
    ("report", "Аналитика: еженедельный отчёт сейчас"),
    ("monthly", "Аналитика: управленческий отчёт за месяц"),
    ("threshold", "Настройки: порог негатива"),
    ("report_time", "Настройки: расписание отчёта"),
    ("report_here", "Настройки: присылать отчёт сюда"),
    ("export", "Настройки: выгрузить в Google Таблицу"),
    ("id", "Настройки: ID этого чата"),
    ("demo", "Помощь: учебные отзывы"),
    ("cancel", "Прочее: отменить ввод"),
]

GUIDE = (
    "📖 <b>Как работать с отзывом</b>\n\n"
    "1. Отзыв приходит карточкой. Негатив — с отметкой 🚨 и ещё в чат «Негатив».\n"
    "2. Нажмите <b>Взять в работу</b> — остальные увидят, что отзыв ваш.\n"
    "3. <b>Подготовить ответ</b> — бот пришлёт черновик. Не нравится — "
    "«Другой вариант», «Короче», «Теплее» или «Официальнее».\n"
    "4. Скопируйте текст, опубликуйте в кабинете площадки (кнопка «Открыть в …») "
    "и отметьте <b>Опубликовал(а) как есть</b> или <b>со своими правками</b>.\n\n"
    "Отвечать не нужно — <b>Без ответа</b>. Это реклама, а не отзыв — <b>🚫 Реклама</b>. "
    "Взяли по ошибке — <b>Вернуть в очередь</b>.\n\n"
    "Яндекс обрезал текст — <b>Добавить полный текст</b> и вставьте его целиком."
)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def root_keyboard() -> InlineKeyboardMarkup:
    keys = list(SECTIONS)
    buttons = [_btn(SECTIONS[k][0], f"mn:{k}") for k in keys]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def section_keyboard(key: str) -> InlineKeyboardMarkup:
    _, _, items = SECTIONS[key]
    rows = [[_btn(label, f"ma:{action}")] for label, action in items]
    rows.append([_btn("← Назад", "mn:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ROOT_TEXT = "📋 <b>Меню бота отзывов</b>\nВыберите раздел:"


def section_text(key: str) -> str:
    title, about, _ = SECTIONS[key]
    return f"<b>{title}</b>\n{about}"


def commands_text() -> str:
    lines = ["⌨️ <b>Все команды</b> — то же, что кнопки меню", ""]
    current = None
    for cmd, desc in COMMANDS[1:]:
        group, _, what = desc.partition(": ")
        if not what:
            group, what = "Прочее", desc
        if group != current:
            lines += ["", f"<b>{group}</b>"] if current else [f"<b>{group}</b>"]
            current = group
        lines.append(f"/{cmd} — {what}")
    return "\n".join(lines)


async def set_commands(bot: Bot) -> None:
    """Список для кнопки «Меню» у поля ввода — в личке и в группах."""
    commands = [BotCommand(command=c, description=d) for c, d in COMMANDS]
    for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats()):
        try:
            await bot.set_my_commands(commands, scope=scope)
        except Exception as e:  # меню команд — удобство, не повод падать
            log.warning("не обновил список команд в Telegram: %s", e)


# --- Открыть меню -------------------------------------------------------------


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(ROOT_TEXT, reply_markup=root_keyboard())


@router.callback_query(F.data.startswith("mn:"))
async def on_section(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 1)[1]
    if key == "root":
        text, kb = ROOT_TEXT, root_keyboard()
    elif key in SECTIONS:
        text, kb = section_text(key), section_keyboard(key)
    else:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        log.debug("меню не обновилось: %s", e)
    await callback.answer()


# --- Действия -------------------------------------------------------------------


def threshold_keyboard(current: int) -> InlineKeyboardMarkup:
    labels = {1: "1★", 2: "1–2★", 3: "1–3★", 4: "1–4★"}
    row = [_btn(("✓ " if n == current else "") + label, f"mt:{n}") for n, label in labels.items()]
    return InlineKeyboardMarkup(inline_keyboard=[row, [_btn("← Назад", "mn:settings")]])


def schedule_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle = _btn("🔕 Выключить отчёт" if enabled else "🔔 Включить отчёт",
                  "mr:off" if enabled else "mr:on")
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Пн 10:00", "mr:пн 10:00"), _btn("Пн 09:00", "mr:пн 9:00"), _btn("Пт 18:00", "mr:пт 18:00")],
        [toggle],
        [_btn("← Назад", "mn:settings")],
    ])


def _default_report_chat():
    from telegram_ui.stats_ui import default_report_chat

    return default_report_chat()


def schedule_text(conn, chat_id) -> str:
    s = analytics.load_schedule(conn, _default_report_chat())
    where = ("в этот чат" if s.chat == str(chat_id) else
             f"в чат <code>{s.chat}</code>" if s.chat else "чат не задан")
    text = f"⏰ <b>Еженедельный отчёт</b>\nСейчас: {s.describe()}"
    if s.enabled:
        text += f", {where}"
    return (text + "\n\nВыберите готовый вариант или задайте свой: "
            "<code>/report_time ср 12:30</code>")


@router.callback_query(F.data.startswith("ma:"))
async def on_action(callback: CallbackQuery, state: FSMContext, conn) -> None:
    import management
    from telegram_ui import bot as B
    from telegram_ui import manage_ui, search_ui, stats_ui

    action = callback.data.split(":", 1)[1]
    msg = callback.message  # ответы уходят в тот же чат, где открыто меню
    await callback.answer()

    if action == "new":
        await B.cmd_new(msg, conn)
    elif action == "negative":
        await B.cmd_negative(msg, conn)
    elif action == "reviews":
        await search_ui.cmd_reviews(msg, conn)
    elif action == "find":
        from telegram_ui.prompts import ask
        from search import Filter

        await state.set_state(search_ui.SearchInput.waiting)
        await state.update_data(filter=Filter().encode())
        await ask(msg, callback.from_user,
                  "пришлите слово из отзыва или имя гостя. Передумали — /cancel",
                  "Например: торт")
    elif action == "spam":
        await search_ui.cmd_spam(msg, conn)
    elif action == "stats":
        await stats_ui.cmd_stats(msg, conn, NO_ARGS)
    elif action == "topics":
        await stats_ui.cmd_topics(msg, conn)
    elif action == "report":
        await stats_ui.cmd_report(msg, conn)
    elif action == "branches":
        await manage_ui.cmd_branches(msg, conn)
    elif action == "dynamics":
        await manage_ui.cmd_dynamics(msg, conn)
    elif action == "monthly":
        await manage_ui.cmd_monthly(msg, conn, NO_ARGS)
    elif action == "monthly_now":
        await manage_ui.cmd_monthly(msg, conn, SimpleNamespace(args="сейчас"))
    elif action == "monthly_settings":
        enabled, _ = management.monthly_schedule(conn)
        await msg.edit_text(manage_ui.monthly_settings_text(conn),
                            reply_markup=manage_ui.monthly_keyboard(enabled))
    elif action == "threshold":
        current = storage.negative_threshold(conn)
        await msg.edit_text(
            f"🎚 <b>Порог негатива</b>\nСейчас негативом считаются отзывы на {current} "
            f"{'звезду' if current == 1 else 'звезды'} и ниже: они получают 🚨 и уходят "
            "в чат «Негатив». Выберите:",
            reply_markup=threshold_keyboard(current))
    elif action == "report_time":
        s = analytics.load_schedule(conn, _default_report_chat())
        await msg.edit_text(schedule_text(conn, msg.chat.id), reply_markup=schedule_keyboard(s.enabled))
    elif action == "report_here":
        await stats_ui.cmd_report_here(msg, conn)
    elif action == "export":
        await B.cmd_export(msg, conn)
    elif action == "id":
        await B.cmd_id(msg)
    elif action == "guide":
        await msg.answer(GUIDE)
    elif action == "commands":
        await msg.answer(commands_text())
    elif action == "demo":
        await B.cmd_demo(msg, conn)
    elif action == "clear_demo":
        await B.cmd_clear_demo(msg, conn)


@router.callback_query(F.data.startswith("mt:"))
async def on_threshold(callback: CallbackQuery, conn) -> None:
    try:
        value = int(callback.data.split(":", 1)[1])
        storage.set_negative_threshold(conn, value)
    except ValueError:
        await callback.answer()
        return
    await callback.answer(f"Готово: негатив — {value}★ и ниже. Уже сохранённые отзывы не меняются",
                          show_alert=True)
    try:
        await callback.message.edit_text(
            f"🎚 <b>Порог негатива</b>\nТеперь негативом считаются отзывы на {value} "
            f"{'звезду' if value == 1 else 'звезды'} и ниже.",
            reply_markup=threshold_keyboard(value))
    except Exception as e:
        log.debug("порог: сообщение не обновилось: %s", e)


@router.callback_query(F.data.startswith("mr:"))
async def on_schedule(callback: CallbackQuery, conn) -> None:
    raw = callback.data.split(":", 1)[1]
    try:
        day, at, enabled = analytics.parse_schedule("выкл" if raw == "off" else
                                                    "вкл" if raw == "on" else raw)
    except ValueError:
        await callback.answer()
        return
    if day is not None:
        storage.set_state(conn, analytics.REPORT_DAY, str(day))
    if at is not None:
        storage.set_state(conn, analytics.REPORT_TIME, at.strftime("%H:%M"))
    if enabled is not None:
        storage.set_state(conn, analytics.REPORT_ENABLED, "1" if enabled else "0")
    s = analytics.load_schedule(conn, _default_report_chat())
    await callback.answer("Готово: " + s.describe())
    try:
        await callback.message.edit_text(schedule_text(conn, callback.message.chat.id),
                                         reply_markup=schedule_keyboard(s.enabled))
    except Exception as e:
        log.debug("расписание: сообщение не обновилось: %s", e)
