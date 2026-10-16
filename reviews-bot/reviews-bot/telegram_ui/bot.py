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
from aiogram.filters import Command, CommandObject
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
        "/negative — негативные отзывы без реакции\n"
        "/threshold — порог негатива (сейчас 1–3 звезды)\n"
        "/demo — учебные отзывы, чтобы попробовать кнопки\n"
        "/id — узнать ID этого чата"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@dp.message(Command("threshold"))
async def cmd_threshold(message: Message, conn, command: CommandObject) -> None:
    """Порог негатива (п. 17 ТЗ): /threshold — показать, /threshold 2 — изменить."""
    current = storage.negative_threshold(conn)
    arg = (command.args or "").strip()

    if not arg:
        await message.answer(
            f"Негативными считаются отзывы с оценкой {current} и ниже.\n"
            f"Изменить: <code>/threshold 2</code>"
        )
        return

    try:
        storage.set_negative_threshold(conn, int(arg))
    except ValueError:
        await message.answer("Нужно число от 1 до 5, например <code>/threshold 2</code>")
        return

    await message.answer(
        f"Готово. Теперь негативными считаются отзывы с оценкой {int(arg)} и ниже.\n"
        "Уже сохранённые отзывы не пересчитываются."
    )


@dp.message(Command("negative"))
async def cmd_negative(message: Message, conn) -> None:
    """Непросмотренные негативные отзывы."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM reviews WHERE is_negative AND status = 'attention' "
            "ORDER BY received_at DESC LIMIT 10"
        )
        ids = [r["id"] for r in cur.fetchall()]

    if not ids:
        await message.answer("Негативных отзывов без реакции нет.")
        return

    for review_id in ids:
        card = card_for(conn, review_id)
        if card:
            await message.answer(
                render_card(card),
                reply_markup=keyboard(card),
                disable_web_page_preview=True,
            )


@dp.message(Command("new"))
async def cmd_new(message: Message, conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM reviews WHERE status IN ('new', 'attention') "
            "ORDER BY received_at DESC LIMIT 10"
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

    # Почта читается фоновой задачей, параллельно с обработкой кнопок.
    # Своё подключение к базе: конвейер и обработчики не должны
    # толкаться в одной транзакции.
    mail_task = None
    if os.environ.get("IMAP_USER"):
        mail_conn = storage.connect(os.environ["DATABASE_URL"])
        mail_task = asyncio.create_task(mail_loop(bot, mail_conn))
    else:
        log.warning("IMAP_USER не задан — почта не читается, работают только кнопки")

    try:
        # conn прокидывается во все обработчики
        await dp.start_polling(bot, conn=conn)
    finally:
        if mail_task:
            mail_task.cancel()


# --- Фоновое чтение почты --------------------------------------------------


async def mail_loop(bot: Bot, conn) -> None:
    """
    Каждые POLL_INTERVAL секунд проверяем ящик. Любая ошибка не останавливает
    цикл: пишем в лог, при необходимости сообщаем администратору и пробуем снова.
    """
    from config import Config

    import pipeline
    from mailbox import Mailbox, MailboxError, parse_cutoff

    cfg = Config.from_env()
    since = parse_cutoff(cfg.imap_since, pipeline.default_since())
    chat = cfg.reviews_chat or cfg.telegram_admin_chat
    log.info("читаю почту %s каждые %s с, письма с %s", cfg.imap_user, cfg.poll_interval, since)

    if not chat:
        log.warning("не задан REVIEWS_CHAT_ID — карточки отправлять некуда")
    if not cfg.negative_chat:
        log.info("NEGATIVE_CHAT_ID не задан — негатив идёт в общий чат с отметкой 🚨")

    last_error: str | None = None
    silence_notified: set[str] = set()

    while True:
        new_ids: list[int] = []
        try:
            with Mailbox(cfg.imap_host, cfg.imap_user, cfg.imap_password, cfg.imap_folder) as box:
                result = await asyncio.to_thread(
                    pipeline.poll_once, conn, box, since,
                    lambda review_id, branch_id: new_ids.append(review_id),
                )
            if result.letters_new or result.errors:
                log.info("проход по почте: %s", result.summary())
            last_error = None
        except MailboxError as e:
            # о проблеме с ящиком сообщаем один раз, а не каждые пять минут
            text = str(e)
            if text != last_error:
                last_error = text
                log.error("ящик недоступен: %s", text)
                await notify_admin(bot, cfg, f"Не читается почта: {text}")
        except Exception:
            log.exception("сбой в проходе по почте")

        for review_id in new_ids:
            await deliver(bot, conn, chat, review_id, cfg.negative_chat)

        await check_silence(bot, conn, cfg, silence_notified)
        await asyncio.sleep(cfg.poll_interval)


async def deliver(bot: Bot, conn, chat: str, review_id: int, negative_chat: str = "") -> None:
    """
    Карточка уходит в чат филиала, если он задан, иначе в общий.

    Негативный отзыв дополнительно дублируется в чат «Негатив» (п. 15 ТЗ) —
    это и есть дополнительное уведомление из п. 7: в общей ленте отзыв легко
    пропустить, а в отдельном чате он один на виду.
    """
    card = card_for(conn, review_id)
    if card is None:
        return

    target = chat
    with conn.cursor() as cur:
        cur.execute(
            "SELECT b.chat_id FROM reviews r JOIN branches b ON b.id = r.branch_id "
            "WHERE r.id = %s",
            (review_id,),
        )
        row = cur.fetchone()
    if row and row["chat_id"]:
        target = row["chat_id"]

    targets = [t for t in (target,) if t]
    if card.is_negative and negative_chat and negative_chat != target:
        targets.append(negative_chat)

    for where in targets:
        try:
            await bot.send_message(
                where,
                render_card(card),
                reply_markup=keyboard(card),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("не отправил карточку отзыва #%s в %s", review_id, where)


async def check_silence(bot: Bot, conn, cfg, notified: set[str]) -> None:
    """
    Молчание площадки выглядит одинаково и когда отзывов нет, и когда бот
    оторвался от почты. Поэтому предупреждаем администратора — один раз
    на площадку, пока не придёт новое письмо.
    """
    import pipeline

    silent = dict(pipeline.silent_sources(conn))
    for source, days in silent.items():
        if source in notified:
            continue
        notified.add(source)
        await notify_admin(
            bot, cfg,
            f"От площадки {source} нет писем уже {days} дн. "
            "Возможно, отзывов правда нет — но стоит проверить настройки уведомлений в кабинете.",
        )
    for source in list(notified):
        if source not in silent:
            notified.discard(source)


async def notify_admin(bot: Bot, cfg, text: str) -> None:
    if not cfg.telegram_admin_chat:
        return
    try:
        await bot.send_message(cfg.telegram_admin_chat, f"⚠️ {text}")
    except Exception:
        log.exception("не отправил сообщение администратору")


def ensure_branches(conn) -> None:
    """
    При каждом запуске сверяем справочник филиалов с seed_branches.py.
    Upsert: существующие не дублируются, новые (например, площадка 2ГИС)
    добавляются сами, без захода в консоль сервера.
    """
    from seed_branches import BRANCHES, SOURCES

    for source in SOURCES:
        for branch in BRANCHES:
            storage.upsert_branch(conn, source, branch["name"], branch["address"])
    conn.commit()
    log.info(
        "справочник филиалов: %s шт. × площадки %s", len(BRANCHES), ", ".join(SOURCES)
    )


if __name__ == "__main__":
    asyncio.run(main())
