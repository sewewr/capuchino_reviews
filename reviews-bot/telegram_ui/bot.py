"""
Telegram-бот: отправка карточек отзывов и работа с ними.

Что умеет:
  — присылает карточку нового отзыва в чат;
  — «Взять в работу» / «Отвечено» / «Без ответа» — смена статуса;
  — «Добавить полный текст» — сотрудник присылает текст, которого нет в письме;
  — карточка обновляется на месте, новых сообщений не плодит.

Запуск: python -m telegram_ui.bot (нужны TELEGRAM_TOKEN и DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import storage
from telegram_ui.cards import TZ as TZ_MSK, CardData, build_keyboard, render_card

log = logging.getLogger(__name__)

dp = Dispatcher()


# --- Состояния -------------------------------------------------------------


class FullText(StatesGroup):
    """Ждём от сотрудника полный текст отзыва."""

    waiting = State()


# --- Сборка клавиатуры -----------------------------------------------------


def keyboard(card: CardData) -> InlineKeyboardMarkup:
    rows = []
    for row in build_keyboard(card):
        buttons = []
        for label, data in row:
            if data.startswith("url:"):
                buttons.append(InlineKeyboardButton(text=label, url=data[4:]))
            else:
                buttons.append(InlineKeyboardButton(text=label, callback_data=data))
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_for(conn, review_id: int) -> CardData | None:
    row = storage.get_card_data(conn, review_id)
    return CardData(**dict(row)) if row else None


# --- Отправка карточки -----------------------------------------------------


async def send_review(bot: Bot, chat_id: str, conn, review_id: int) -> None:
    """Отправить карточку отзыва в чат. Вызывается из пайплайна сбора."""
    card = card_for(conn, review_id)
    if card is None:
        log.warning("отзыв %s не найден", review_id)
        return
    await bot.send_message(
        chat_id,
        render_card(card),
        reply_markup=keyboard(card),
        disable_web_page_preview=True,
    )


async def refresh(callback: CallbackQuery, conn, review_id: int) -> None:
    """Перерисовать карточку на месте после изменения статуса."""
    card = card_for(conn, review_id)
    if card is None:
        return
    try:
        await callback.message.edit_text(
            render_card(card),
            reply_markup=keyboard(card),
            disable_web_page_preview=True,
        )
    except Exception as e:  # текст не изменился или сообщение слишком старое
        log.debug("не удалось обновить карточку %s: %s", review_id, e)


# --- Команды ---------------------------------------------------------------


@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Бот собирает отзывы с площадок и присылает их сюда.\n\n"
        "Когда приходит новый отзыв, нажмите «Взять в работу» — "
        "остальные увидят, что им уже занимаются.\n\n"
        "Если Яндекс обрезал текст, нажмите «Добавить полный текст» "
        "и пришлите его целиком — так он сохранится в базе.\n\n"
        "Команды:\n"
        "/new — показать отзывы, которые ещё никто не взял\n"
        "/demo — учебные отзывы, чтобы попробовать кнопки\n"
        "/id — узнать ID этого чата"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@dp.message(Command("new"))
async def cmd_new(message: Message, conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM reviews WHERE status = 'new' ORDER BY received_at DESC LIMIT 10"
        )
        ids = [r["id"] for r in cur.fetchall()]

    if not ids:
        await message.answer("Новых отзывов нет.")
        return

    for review_id in ids:
        card = card_for(conn, review_id)
        if card:
            await message.answer(
                render_card(card),
                reply_markup=keyboard(card),
                disable_web_page_preview=True,
            )


@dp.message(Command("demo"))
async def cmd_demo(message: Message, conn) -> None:
    """
    Показать интерфейс до того, как подключена почта.
    Создаёт два учебных отзыва и присылает их карточками.
    Удалить: /clear_demo
    """
    from datetime import datetime, timedelta

    from parsers.yandex_business import Review, make_dedup_key

    now = datetime.now(TZ_MSK)
    samples = [
        Review(
            branch_name="Капучино",
            branch_address="Волгоград, Рабоче-Крестьянская улица, 9Б",
            rating=5,
            rating_confident=True,
            text=(
                "Моё самое любимое место, как сладкоежки, обожаю всё, спасибо тем, "
                "кто отвечает мне в соц.сетях о КБЖУ и составе блюд\n"
                "Люблю безмерно 🫶🏻\n"
                "Мой самый любимый торт — Роскошная Блондинка, кто не пробовал, оч..."
            ),
            text_truncated=True,
            received_at=now - timedelta(hours=3),
            warnings=["УЧЕБНЫЙ ОТЗЫВ"],
        ),
        Review(
            branch_name="Капучино",
            branch_address="Волгоград, улица Рокоссовского, 54",
            rating=2,
            rating_confident=False,
            text="Ждали заказ 40 минут, кофе принесли холодный.",
            received_at=now - timedelta(hours=1),
            warnings=["УЧЕБНЫЙ ОТЗЫВ"],
        ),
    ]

    for review in samples:
        review.dedup_key = make_dedup_key(review)
        result = storage.save_review(conn, review)
        card = card_for(conn, result.review_id)
        if card:
            await message.answer(
                render_card(card),
                reply_markup=keyboard(card),
                disable_web_page_preview=True,
            )

    await message.answer(
        "Это учебные отзывы — можно нажимать любые кнопки.\n"
        "Убрать их из базы: /clear_demo"
    )


@dp.message(Command("clear_demo"))
async def cmd_clear_demo(message: Message, conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reviews WHERE 'УЧЕБНЫЙ ОТЗЫВ' = ANY(warnings)")
        removed = cur.rowcount
    conn.commit()
    await message.answer(f"Удалено учебных отзывов: {removed}")


# --- Кнопки ----------------------------------------------------------------


STATUS_BY_ACTION = {
    "take": ("in_progress", "Взяли в работу"),
    "done": ("answered", "Отмечено как отвеченный"),
    "skip": ("skipped", "Отмечено «без ответа»"),
}


@dp.callback_query(F.data.startswith("rv:"))
async def on_button(callback: CallbackQuery, state: FSMContext, conn) -> None:
    _, action, raw_id = callback.data.split(":", 2)
    review_id = int(raw_id)

    if action in STATUS_BY_ACTION:
        status, note = STATUS_BY_ACTION[action]
        storage.set_status(conn, review_id, status)
        await callback.answer(note)
        await refresh(callback, conn, review_id)
        return

    if action == "fulltext":
        await state.set_state(FullText.waiting)
        await state.update_data(review_id=review_id)
        await callback.answer()
        await callback.message.reply(
            f"Пришлите полный текст отзыва #{review_id} ответом на это сообщение.\n"
            "Можно добавить имя автора и дату публикации отдельными строками:\n"
            "<code>Автор: Мария К.</code>\n"
            "<code>Дата: 26.08.2026</code>\n\n"
            "Чтобы отменить — /cancel"
        )
        return

    if action == "draft":
        # Черновик ответа — этап 2 по ТЗ, пока заглушка.
        await callback.answer("Черновики ответов появятся на втором этапе", show_alert=True)
        return

    await callback.answer("Неизвестная кнопка")


# --- Приём полного текста --------------------------------------------------


@dp.message(Command("cancel"), FullText.waiting)
async def cancel_fulltext(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@dp.message(FullText.waiting)
async def receive_fulltext(message: Message, state: FSMContext, conn) -> None:
    data = await state.get_data()
    review_id = data.get("review_id")
    if not review_id:
        await state.clear()
        return

    text, author, published = parse_fulltext(message.text or "")
    if not text:
        await message.answer("Пустой текст — пришлите отзыв целиком или /cancel")
        return

    storage.add_full_text(conn, review_id, text, author=author, published_at=published)
    await state.clear()

    added = ["полный текст"]
    if author:
        added.append("автор")
    if published:
        added.append("дата публикации")
    await message.answer(f"Сохранено для отзыва #{review_id}: {', '.join(added)}.")


def parse_fulltext(raw: str) -> tuple[str, str | None, str | None]:
    """
    Из присланного сообщения вынимаем текст отзыва и необязательные
    строки «Автор:» и «Дата:». Всё остальное считается текстом отзыва.
    """
    author = published = None
    body: list[str] = []

    for line in raw.splitlines():
        low = line.strip().lower()
        if low.startswith("автор:"):
            author = line.split(":", 1)[1].strip() or None
        elif low.startswith("дата:"):
            published = normalize_date(line.split(":", 1)[1].strip())
        else:
            body.append(line)

    return "\n".join(body).strip(), author, published


def normalize_date(raw: str) -> str | None:
    """Принимаем 26.08.2026 и 2026-08-26, в базу кладём ISO."""
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            from datetime import datetime

            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --- Запуск ----------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("не задана переменная TELEGRAM_TOKEN")

    conn = storage.connect(os.environ["DATABASE_URL"])
    storage.init_schema(conn)
    ensure_branches(conn)

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # conn прокидывается во все обработчики
    await dp.start_polling(bot, conn=conn)


def ensure_branches(conn) -> None:
    """
    При первом запуске заливаем справочник филиалов, чтобы не ходить
    в консоль сервера. Повторные запуски ничего не ломают: адреса
    добавляются через upsert.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM branches")
        if cur.fetchone()["n"]:
            return

    from seed_branches import BRANCHES, SOURCES

    for source in SOURCES:
        for branch in BRANCHES:
            storage.upsert_branch(conn, source, branch["name"], branch["address"])
    conn.commit()
    log.info("справочник филиалов залит: %s шт.", len(BRANCHES))


if __name__ == "__main__":
    asyncio.run(main())
