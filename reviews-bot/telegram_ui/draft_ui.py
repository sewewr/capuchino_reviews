"""
Черновики ответов в Telegram (п. 9 ТЗ).

Сотрудник жмёт «Подготовить ответ» на карточке — бот присылает отдельным
сообщением черновик от GigaChat. Под ним кнопки из ТЗ: другой вариант,
короче, теплее, официальнее. Текст в блоке, который копируется одним нажатием.

Когда ответ опубликован на площадке, сотрудник отмечает это:
  — «Опубликовал(а) как есть» — сохраняется последний черновик;
  — «Опубликовал(а) со своими правками» — бот просит прислать итоговый текст.
Итоговый ответ сохраняется в базу и уходит в колонку «Ответ» Google Таблицы.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import drafts
import storage

log = logging.getLogger(__name__)
router = Router()

# Двойное нажатие не должно запускать две генерации подряд.
_busy: set[int] = set()

STYLE_BUTTONS = [
    [("🔄 Другой вариант", "another"), ("✂️ Короче", "shorter")],
    [("🤗 Теплее", "warmer"), ("👔 Официальнее", "formal")],
]

SHORT_SOURCE = {"yandex": "Яндексе", "2gis": "2ГИС", "tripadvisor": "Tripadvisor"}


class OwnReply(StatesGroup):
    """Ждём текст, который сотрудник опубликовал со своими правками."""

    waiting = State()


def draft_keyboard(review_id: int, reply_url: str | None, source: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"dr:{style}:{review_id}")
         for label, style in row]
        for row in STYLE_BUTTONS
    ]
    if reply_url:
        where = SHORT_SOURCE.get(source, "кабинете")
        rows.append([InlineKeyboardButton(text=f"Открыть в {where}", url=reply_url)])
    rows.append([InlineKeyboardButton(text="✅ Опубликовал(а) как есть",
                                      callback_data=f"dr:done:{review_id}")])
    rows.append([InlineKeyboardButton(text="✏️ Опубликовал(а) со своими правками",
                                      callback_data=f"dr:own:{review_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_draft(review: dict, draft: drafts.Draft) -> str:
    stars = f"{review['rating']}★" if review["rating"] is not None else "оценка ?"
    source = drafts.SOURCE_NAMES.get(review["source"], review["source"])
    lines = [
        f"<b>✍️ Черновик ответа</b> · отзыв #{review['review_id']}",
        f"{html.escape(source)} · {stars}",
        "",
        f"<pre>{html.escape(draft.text)}</pre>",
    ]
    for w in draft.warnings:
        lines.append(f"⚠️ {html.escape(w)}")
    lines += [
        "",
        "Скопируйте текст, опубликуйте в кабинете площадки и отметьте ниже.",
    ]
    return "\n".join(lines)


def _review(conn, review_id: int) -> drafts.ReviewForDraft | None:
    row = storage.review_for_draft(conn, review_id)
    if row is None:
        return None
    return drafts.ReviewForDraft(**dict(row))


async def _generate(conn, review_id: int, style: str, user) -> tuple[dict, drafts.Draft]:
    """Получить черновик и сохранить его. Ошибки — DraftError."""
    review = _review(conn, review_id)
    if review is None:
        raise drafts.DraftError("отзыв не найден")

    previous = None
    if style != "base":
        last = storage.latest_draft(conn, review_id)
        if last is None:
            raise drafts.DraftError("сначала нужен первый черновик")
        previous = last["text"]

    recent = [drafts.opening(t) for t in
              storage.recent_draft_texts(conn, drafts.RECENT_OPENINGS, exclude_review=review_id)]

    draft = await asyncio.to_thread(
        drafts.generate, review, style, previous, recent,
    )
    storage.save_draft(conn, review_id, draft.text, style,
                       user.id if user else None, user.full_name if user else None)
    return storage.review_for_draft(conn, review_id) | {"reply_url": _reply_url(conn, review_id)}, draft


def _reply_url(conn, review_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT reply_url FROM reviews WHERE id = %s", (review_id,))
        row = cur.fetchone()
    return row["reply_url"] if row else None


# --- Запуск с карточки ------------------------------------------------------


async def start_draft(callback: CallbackQuery, conn, review_id: int) -> None:
    """Вызывается из обработчика кнопок карточки (rv:draft:N)."""
    if not drafts.enabled():
        await callback.answer(
            "Черновики не подключены: администратору нужно задать GIGACHAT_CREDENTIALS.",
            show_alert=True,
        )
        return
    if review_id in _busy:
        await callback.answer("Уже готовлю черновик…")
        return

    await callback.answer()
    wait = await callback.message.reply("✍️ Готовлю черновик…")
    _busy.add(review_id)
    try:
        review, draft = await _generate(conn, review_id, "base", callback.from_user)
    except drafts.DraftError as e:
        await wait.edit_text(f"Не получилось подготовить черновик: {html.escape(str(e))}")
        return
    finally:
        _busy.discard(review_id)

    await wait.edit_text(
        render_draft(review, draft),
        reply_markup=draft_keyboard(review_id, review.get("reply_url"), review["source"]),
    )


# --- Кнопки под черновиком ----------------------------------------------------


@router.callback_query(F.data.startswith("dr:"))
async def on_draft_button(callback: CallbackQuery, state: FSMContext, conn) -> None:
    _, action, raw_id = callback.data.split(":", 2)
    review_id = int(raw_id)

    if action in drafts.STYLES:
        if review_id in _busy:
            await callback.answer("Уже готовлю…")
            return
        await callback.answer("Переписываю…")
        _busy.add(review_id)
        try:
            review, draft = await _generate(conn, review_id, action, callback.from_user)
        except drafts.DraftError as e:
            await callback.message.answer(f"Не получилось: {html.escape(str(e))}")
            return
        finally:
            _busy.discard(review_id)
        await callback.message.edit_text(
            render_draft(review, draft),
            reply_markup=draft_keyboard(review_id, review.get("reply_url"), review["source"]),
        )
        return

    if action == "done":
        last = storage.latest_draft(conn, review_id)
        if last is None:
            await callback.answer("Черновика нет", show_alert=True)
            return
        await _finish(callback, conn, review_id, last["text"])
        await callback.answer("Ответ сохранён")
        return

    if action == "own":
        await state.set_state(OwnReply.waiting)
        await state.update_data(review_id=review_id)
        await callback.answer()
        await callback.message.reply(
            f"Пришлите текст ответа на отзыв #{review_id} в том виде, "
            "в каком вы его опубликовали.\nОтменить — /cancel"
        )
        return

    await callback.answer("Неизвестная кнопка")


@router.message(Command("cancel"), OwnReply.waiting)
async def cancel_own_reply(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(OwnReply.waiting, F.text, ~F.text.startswith("/"))
async def receive_own_reply(message: Message, state: FSMContext, conn) -> None:
    data = await state.get_data()
    review_id = data.get("review_id")
    await state.clear()
    if not review_id:
        return

    text = message.text.strip()
    storage.save_draft(conn, review_id, text, "manual",
                       message.from_user.id, message.from_user.full_name)
    await _finish(None, conn, review_id, text, bot=message.bot, user=message.from_user)
    await message.answer(f"Сохранила ответ на отзыв #{review_id}, отзыв отмечен обработанным.")


async def _finish(callback: CallbackQuery | None, conn, review_id: int, text: str,
                  bot=None, user=None) -> None:
    """Ответ опубликован: сохраняем текст, закрываем отзыв, обновляем все копии карточки."""
    from telegram_ui.bot import display_name, refresh_everywhere

    user = user or callback.from_user
    bot = bot or callback.bot
    storage.set_reply(conn, review_id, text)
    storage.close_review(conn, review_id, "answered", user.id, display_name(user))
    await refresh_everywhere(bot, conn, review_id)

    if callback is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply("✅ Ответ сохранён, отзыв отмечен обработанным.")
        except Exception as e:
            log.debug("не обновил сообщение с черновиком: %s", e)
