"""
Фильтры и поиск в Telegram (п. 11 ТЗ).

/reviews        — список отзывов с кнопками-фильтрами
/find <слово>   — поиск по тексту или имени гостя

Список — одно сообщение, которое перерисовывается на месте при нажатии
фильтров. Номер отзыва — кнопка: она присылает полноценную карточку,
с которой можно работать как обычно.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import search
from search import Filter

log = logging.getLogger(__name__)

router = Router(name="search")


class SearchInput(StatesGroup):
    waiting = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _mark(label: str, active: bool) -> str:
    return f"✓ {label}" if active else label


def list_keyboard(page: search.Page, conn) -> InlineKeyboardMarkup:
    f = page.filter
    rows = [
        [_btn(_mark(label, f.period == code), f.with_(period=code).encode())
         for code, label in search.PERIODS.items()],
        # повторное нажатие на активный статус снимает его
        [_btn(_mark(label, f.status == code),
              f.with_(status="a" if f.status == code else code).encode())
         for code, label in search.STATUSES.items() if code != "a"],
    ]

    branch_label = ("🏠 " + search.branch_name(conn, f.branch)) if f.branch else "🏠 Все филиалы"
    order = list(search.SOURCES)
    next_source = order[(order.index(f.source) + 1) % len(order)]
    rows.append([
        _btn(branch_label + " ▾", f.encode("fb")),
        _btn("📍 " + search.SOURCES[f.source], f.with_(source=next_source).encode()),
    ])

    numbers = [_btn(f"#{r.review_id}", f"fo:{r.review_id}") for r in page.rows]
    rows += [numbers[i:i + 4] for i in range(0, len(numbers), 4)]

    if page.pages > 1:
        nav = []
        if f.page > 0:
            nav.append(_btn("◀", f.with_(page=f.page - 1).encode()))
        nav.append(_btn(f"{f.page + 1} / {page.pages}", f.encode()))
        if f.page < page.pages - 1:
            nav.append(_btn("▶", f.with_(page=f.page + 1).encode()))
        rows.append(nav)

    last = [_btn("🔍 Поиск", f.encode("fs"))]
    if not f.is_default:
        last.append(_btn("✖ Сбросить", Filter().encode()))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def branch_keyboard(f: Filter, conn) -> InlineKeyboardMarkup:
    choices = search.branch_choices(conn)
    buttons = [_btn(_mark(name, f.branch == key), f.with_(branch=key).encode())
               for key, name in choices]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_btn(_mark("Филиал не определён", f.branch == "u"), f.with_(branch="u").encode()),
                 _btn(_mark("Все филиалы", not f.branch), f.with_(branch="").encode())])
    rows.append([_btn("← Назад", f.encode())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build(conn, f: Filter) -> tuple[str, InlineKeyboardMarkup]:
    page = search.fetch(conn, f)
    return search.render(page, conn), list_keyboard(page, conn)


# --- Команды ------------------------------------------------------------------


@router.message(Command("reviews"))
async def cmd_reviews(message: Message, conn) -> None:
    text, kb = build(conn, Filter())
    await message.answer(text, reply_markup=kb)


@router.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext, conn, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await state.set_state(SearchInput.waiting)
        await state.update_data(filter=Filter().encode())
        await message.answer("Пришлите слово из отзыва или имя гостя. Передумали — /cancel")
        return
    await _run_search(message, conn, Filter(), arg)


async def _run_search(message: Message, conn, base: Filter, text: str) -> None:
    if len(text) < 2:
        await message.answer("Слишком коротко — нужно хотя бы 2 буквы.")
        return
    key = search.save_query(conn, text)
    out, kb = build(conn, base.with_(query=key))
    await message.answer(out, reply_markup=kb)


@router.message(Command("cancel"), SearchInput.waiting)
async def cancel_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, отменил.")


@router.message(SearchInput.waiting, F.text & ~F.text.startswith("/"))
async def receive_search(message: Message, state: FSMContext, conn) -> None:
    data = await state.get_data()
    await state.clear()
    base = Filter.decode(data.get("filter", ""))
    await _run_search(message, conn, base, message.text.strip())


# --- Кнопки -------------------------------------------------------------------


@router.callback_query(F.data.startswith("fl:"))
async def on_filter(callback: CallbackQuery, conn) -> None:
    text, kb = build(conn, Filter.decode(callback.data))
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:  # «сообщение не изменилось» — нажали активную кнопку
        log.debug("список не обновился: %s", e)
    await callback.answer()


@router.callback_query(F.data.startswith("fb:"))
async def on_branch_picker(callback: CallbackQuery, conn) -> None:
    f = Filter.decode(callback.data)
    try:
        await callback.message.edit_reply_markup(reply_markup=branch_keyboard(f, conn))
    except Exception as e:
        log.debug("выбор филиала не открылся: %s", e)
    await callback.answer("Выберите филиал")


@router.callback_query(F.data.startswith("fs:"))
async def on_search_button(callback: CallbackQuery, state: FSMContext) -> None:
    f = Filter.decode(callback.data)
    await state.set_state(SearchInput.waiting)
    await state.update_data(filter=f.with_(query="").encode())
    await callback.answer()
    hint = "" if f.with_(query="").is_default else " Текущие фильтры сохранятся."
    await callback.message.answer(
        f"Пришлите слово из отзыва или имя гостя.{hint} Передумали — /cancel")


@router.callback_query(F.data.startswith("fo:"))
async def on_open(callback: CallbackQuery, conn) -> None:
    from telegram_ui.bot import post_card

    try:
        review_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    await callback.answer()
    await post_card(callback.bot, conn, callback.message.chat.id, review_id)
