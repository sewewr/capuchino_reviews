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


def _include_routers() -> None:
    """Подключаем модули с кнопками. Отдельно, чтобы избежать циклического импорта."""
    from telegram_ui import draft_ui, manage_ui, menu_ui, search_ui, stats_ui

    for module in (menu_ui, draft_ui, stats_ui, search_ui, manage_ui):
        if module.router.parent_router is None:
            dp.include_router(module.router)


# --- Состояния -------------------------------------------------------------


class FullText(StatesGroup):
    """Ждём от сотрудника полный текст отзыва."""

    waiting = State()


class CommentInput(StatesGroup):
    """Ждём внутренний комментарий к отзыву."""

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
    if not row:
        return None
    return CardData(**dict(row), comments=storage.comments(conn, review_id))


# --- Отправка карточки -----------------------------------------------------


async def post_card(bot: Bot, conn, chat_id, review_id: int) -> None:
    """
    Отправить карточку и запомнить, куда она ушла. Все карточки отправляются
    только через эту функцию — иначе при смене статуса какая-то копия
    останется необновлённой.
    """
    card = card_for(conn, review_id)
    if card is None:
        log.warning("отзыв %s не найден", review_id)
        return
    try:
        sent = await bot.send_message(
            chat_id,
            render_card(card),
            reply_markup=keyboard(card),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("не отправил карточку отзыва #%s в %s", review_id, chat_id)
        return
    storage.remember_message(conn, review_id, chat_id, sent.message_id)


async def refresh_everywhere(bot: Bot, conn, review_id: int, current=None) -> None:
    """
    Перерисовать все копии карточки после смены статуса: в общем чате,
    в чате «Негатив», в ответах на /new. current — сообщение, на котором
    нажали кнопку: оно обновляется, даже если было отправлено до того, как
    бот начал запоминать копии.
    """
    card = card_for(conn, review_id)
    if card is None:
        return

    targets = set(storage.review_messages(conn, review_id))
    if current is not None:
        targets.add((str(current.chat.id), current.message_id))

    text, markup = render_card(card), keyboard(card)
    for chat_id, message_id in targets:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id,
                reply_markup=markup, disable_web_page_preview=True,
            )
        except Exception as e:
            # «текст не изменился» или сообщение удалили — не страшно
            log.debug("не обновил копию #%s в %s: %s", review_id, chat_id, e)


# --- Команды ---------------------------------------------------------------


@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    """Коротко о боте и сразу меню: команды запоминать не нужно."""
    from telegram_ui.menu_ui import root_keyboard

    await message.answer(
        "Бот собирает отзывы с Яндекс Карт и 2ГИС и присылает их сюда карточками.\n\n"
        "Всё, что умеет бот, — в меню ниже. Открыть его снова: /menu "
        "или кнопка «Меню» слева от поля ввода.",
        reply_markup=root_keyboard(),
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@dp.message(Command("export"))
async def cmd_export(message: Message, conn) -> None:
    """Выгрузить в Google Таблицу прямо сейчас, не дожидаясь прохода по почте."""
    from config import Config

    import sheets

    cfg = Config.from_env()
    if not (cfg.google_credentials and cfg.google_sheet_id):
        await message.answer(
            "Google Таблица не подключена.\n"
            "Нужны переменные <code>GOOGLE_CREDENTIALS_JSON</code> и "
            "<code>GOOGLE_SHEET_ID</code>."
        )
        return

    await message.answer("Выгружаю…")
    try:
        result = await asyncio.to_thread(
            sheets.export, conn, cfg.google_credentials,
            cfg.google_sheet_id, cfg.google_sheet_name,
        )
    except sheets.SheetsError as e:
        await message.answer(f"Не получилось: {e}")
        return

    if result.written:
        await message.answer(f"Готово, строк в таблице: {result.rows}")
    else:
        await message.answer(
            f"Данные не менялись с прошлой выгрузки, в таблице {result.rows} строк."
        )


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
        await post_card(message.bot, conn, message.chat.id, review_id)


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
        await post_card(message.bot, conn, message.chat.id, review_id)


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
        await post_card(message.bot, conn, message.chat.id, result.review_id)

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


def display_name(user) -> str:
    """Как подписать сотрудника в карточке."""
    if user.full_name:
        return user.full_name
    return f"@{user.username}" if user.username else f"id{user.id}"


@dp.callback_query(F.data.startswith("rv:"))
async def on_button(callback: CallbackQuery, state: FSMContext, conn) -> None:
    _, action, raw_id = callback.data.split(":", 2)
    review_id = int(raw_id)
    user = callback.from_user
    name = display_name(user)

    if action == "take":
        won, holder = storage.claim_review(conn, review_id, user.id, name)
        if won:
            await callback.answer("Отзыв ваш")
        else:
            # Кто-то успел раньше — например, в соседнем чате.
            await callback.answer(
                f"Отзыв уже в работе у: {holder or 'другого сотрудника'}",
                show_alert=True,
            )
        await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        return

    if action in ("done", "skip"):
        status = "answered" if action == "done" else "skipped"
        storage.close_review(conn, review_id, status, user.id, name)
        await callback.answer("Отмечено как отвеченный" if action == "done"
                              else "Отмечено «без ответа»")
        await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        return

    if action == "release":
        storage.release_review(conn, review_id)
        await callback.answer("Вернули в очередь")
        await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        return

    if action == "spam":
        storage.mark_spam(conn, review_id, user.id, name)
        await callback.answer("Скрыто как реклама — из статистики и списков убрано")
        await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        return

    if action == "notspam":
        storage.unmark_spam(conn, review_id)
        await callback.answer("Вернули — отзыв снова в работе")
        await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        # Если бот спрятал отзыв сразу, в общий чат он не попадал — отправим.
        chat = os.environ.get("REVIEWS_CHAT_ID") or os.environ.get("TELEGRAM_ADMIN_CHAT", "")
        sent_to = {c for c, _ in storage.review_messages(conn, review_id)}
        if chat and chat not in sent_to:
            await deliver(callback.bot, conn, chat, review_id,
                          os.environ.get("NEGATIVE_CHAT_ID", ""))
        return

    if action == "comment":
        from telegram_ui.prompts import ask

        await state.set_state(CommentInput.waiting)
        await state.update_data(review_id=review_id)
        await callback.answer()
        await ask(callback.message, callback.from_user,
                  f"напишите комментарий к отзыву #{review_id}. Его увидят только "
                  "сотрудники — на площадку он не уходит.\nОтменить — /cancel",
                  "Например: позвонили гостю, извинились", reply=True)
        return

    if action == "problem":
        if storage.mark_problem(conn, review_id, name):
            await callback.answer("Отмечен проблемным — теперь он в негативе", show_alert=False)
            await refresh_everywhere(callback.bot, conn, review_id, callback.message)
            # как и негатив по оценке — дубль в чат «Негатив», если он есть
            neg = os.environ.get("NEGATIVE_CHAT_ID", "")
            sent_to = {c for c, _ in storage.review_messages(conn, review_id)}
            if neg and neg not in sent_to:
                await post_card(callback.bot, conn, neg, review_id)
        else:
            await callback.answer("Этот отзыв уже в негативе или закрыт")
            await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        return

    if action == "unproblem":
        if storage.unmark_problem(conn, review_id):
            await callback.answer("Отметка «Проблемный» снята")
        else:
            await callback.answer("Отметки уже нет")
        await refresh_everywhere(callback.bot, conn, review_id, callback.message)
        return

    if action == "fulltext":
        await state.set_state(FullText.waiting)
        await state.update_data(review_id=review_id)
        await callback.answer()
        from telegram_ui.prompts import ask

        await ask(
            callback.message, callback.from_user,
            f"пришлите полный текст отзыва #{review_id} ответом на это сообщение.\n"
            "Можно добавить имя автора и дату публикации отдельными строками:\n"
            "<code>Автор: Мария К.</code>\n"
            "<code>Дата: 26.08.2026</code>\n\n"
            "Чтобы отменить — /cancel",
            "Полный текст отзыва", reply=True,
        )
        return

    if action == "draft":
        from telegram_ui import draft_ui

        await draft_ui.start_draft(callback, conn, review_id)
        return

    await callback.answer("Неизвестная кнопка")


# --- Приём полного текста --------------------------------------------------


@dp.message(Command("cancel"), CommentInput.waiting)
async def cancel_comment(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@dp.message(CommentInput.waiting, F.text & ~F.text.startswith("/"))
async def receive_comment(message: Message, state: FSMContext, conn) -> None:
    data = await state.get_data()
    await state.clear()
    review_id = data.get("review_id")
    if not review_id:
        return
    try:
        storage.add_comment(conn, review_id, message.from_user.id,
                            display_name(message.from_user), message.text)
    except ValueError:
        await message.answer("Пустой комментарий не сохранён.")
        return
    await refresh_everywhere(message.bot, conn, review_id)
    await message.answer(f"💬 Комментарий к отзыву #{review_id} сохранён — он виден в карточке "
                         "и в Google Таблице.")


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
    fixed = storage.rematch_unassigned(conn)
    if fixed:
        log.info("филиал определён задним числом для отзывов: %s", fixed)

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _include_routers()

    from telegram_ui.menu_ui import set_commands

    await set_commands(bot)

    # Реклама, пришедшая до появления фильтра: прячем и перерисовываем
    # уже отправленные карточки, чтобы на них не тратили время.
    hidden = storage.hide_existing_spam(conn)
    for review_id in hidden:
        await refresh_everywhere(bot, conn, review_id)
    if hidden:
        log.info("скрыто как реклама задним числом: %s", hidden)

    # Почта читается фоновой задачей, параллельно с обработкой кнопок.
    # Своё подключение к базе: конвейер и обработчики не должны
    # толкаться в одной транзакции.
    mail_task = None
    if os.environ.get("IMAP_USER"):
        mail_conn = storage.connect(os.environ["DATABASE_URL"])
        mail_task = asyncio.create_task(mail_loop(bot, mail_conn))
    else:
        log.warning("IMAP_USER не задан — почта не читается, работают только кнопки")

    # Еженедельный отчёт (п. 13 ТЗ) — своя задача и своё подключение.
    from telegram_ui import stats_ui

    admin_chat = os.environ.get("TELEGRAM_ADMIN_CHAT", "")

    async def alert(text: str) -> None:
        if admin_chat:
            try:
                await bot.send_message(admin_chat, f"⚠️ {text}")
            except Exception:
                log.exception("не отправил сообщение администратору")

    report_conn = storage.connect(os.environ["DATABASE_URL"])
    report_task = asyncio.create_task(stats_ui.report_loop(bot, report_conn, alert))

    try:
        # conn прокидывается во все обработчики
        await dp.start_polling(bot, conn=conn)
    finally:
        report_task.cancel()
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
    last_sheets_error: str | None = None

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

        last_sheets_error = await sync_sheet(bot, conn, cfg, last_sheets_error)
        await check_silence(bot, conn, cfg)
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

    # Реклама в рабочие чаты не идёт — только администратору, чтобы
    # ошибку фильтра можно было исправить кнопкой «Это не реклама».
    if card.status == "spam":
        admin = os.environ.get("TELEGRAM_ADMIN_CHAT", "")
        if admin:
            await post_card(bot, conn, admin, review_id)
        log.info("отзыв #%s похож на рекламу — в рабочие чаты не отправлен", review_id)
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
        await post_card(bot, conn, where, review_id)


async def sync_sheet(bot: Bot, conn, cfg, last_error: str | None) -> str | None:
    """
    Выгрузка в Google Таблицу (п. 10 ТЗ). Таблица — витрина, а не хранилище:
    если выгрузка не удалась, отзывы всё равно в базе и в Telegram, поэтому
    сбой только логируется и один раз сообщается администратору.
    """
    if not (cfg.google_credentials and cfg.google_sheet_id):
        return None

    import sheets

    try:
        result = await asyncio.to_thread(
            sheets.export, conn, cfg.google_credentials,
            cfg.google_sheet_id, cfg.google_sheet_name,
        )
        if result.written:
            log.info("Google Таблица обновлена, строк: %s", result.rows)
        return None
    except sheets.SheetsError as e:
        text = str(e)
        if text != last_error:
            log.error("не выгрузилось в Google Таблицу: %s", text)
            await notify_admin(bot, cfg, f"Не выгружается в Google Таблицу: {text}")
        return text
    except Exception:
        log.exception("сбой выгрузки в Google Таблицу")
        return last_error


SILENCE_KEY = "silence_notified:"  # + площадка → дата последнего письма, о которой уже сказали


async def check_silence(bot: Bot, conn, cfg, notified=None) -> None:
    """
    Молчание площадки выглядит одинаково и когда отзывов нет, и когда бот
    оторвался от почты. Поэтому предупреждаем администратора — один раз
    на одно затишье, пока не придёт новое письмо.

    Что уже предупредили, храним в базе, а не в памяти: иначе каждый
    перезапуск (а Railway перезапускает бота при каждой заливке на GitHub)
    присылал бы то же предупреждение заново.
    """
    import pipeline

    silent = dict(pipeline.silent_sources(conn))
    for source, days in silent.items():
        last = storage.last_letter_at(conn, source)
        marker = last.isoformat() if last else ""
        key = SILENCE_KEY + source
        if storage.get_state(conn, key) == marker:
            continue
        storage.set_state(conn, key, marker)
        name = {"yandex": "Яндекса", "2gis": "2ГИС"}.get(source, source)
        await notify_admin(
            bot, cfg,
            f"От {name} нет писем об отзывах уже {days} дн. Возможно, отзывов правда "
            "нет — но стоит проверить настройки уведомлений в кабинете площадки. "
            "Следующее предупреждение придёт, только если площадка снова замолчит "
            "после нового письма.",
        )


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
