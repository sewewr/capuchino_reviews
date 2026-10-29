"""
Сборка карточки отзыва для Telegram (вариант Б: рабочий).

Формат карточки — чистая функция: её видно в тестах без запуска бота.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Moscow")

SOURCE_NAMES = {
    "yandex": "Яндекс Карты",
    "2gis": "2ГИС",
    "tripadvisor": "Tripadvisor",
}

# Как площадка называется в кнопке и предупреждениях
SOURCE_SHORT = {
    "yandex": "Яндексе",
    "2gis": "2ГИС",
    "tripadvisor": "Tripadvisor",
}
SOURCE_BY = {
    "yandex": "Яндексом",
    "2gis": "2ГИС",
    "tripadvisor": "Tripadvisor",
}

STATUS_MARKS = {
    "new": "🟢 НОВЫЙ ОТЗЫВ",
    "attention": "🚨 ТРЕБУЕТ ВНИМАНИЯ",
    "in_progress": "🟡 В РАБОТЕ",
    "answered": "✅ ОТВЕЧЕНО",
    "skipped": "⚪️ БЕЗ ОТВЕТА",
}

ASSIGNEE_LABELS = {
    "in_progress": "В работе у",
    "answered": "Ответил(а)",
    "skipped": "Закрыл(а)",
}

# Статусы, при которых отзыв считается незакрытым и показывает рабочие кнопки.
OPEN_STATUSES = ("new", "attention")

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


@dataclass
class CardData:
    """Всё, что нужно для карточки. Собирается из базы, не из письма."""

    review_id: int
    source: str
    rating: int | None
    rating_confident: bool
    text: str
    text_full: str | None
    text_truncated: bool
    author: str | None
    published_at: object | None
    received_at: datetime
    reply_url: str | None
    status: str
    possible_duplicate: bool
    duplicate_of: int | None
    branch_name: str | None
    branch_address: str | None
    assignee: str | None = None
    is_negative: bool = False  # оценка не выше порога негатива


def render_stars(rating: int | None) -> str:
    if rating is None:
        return "не определена"
    filled = "⭐️" * rating
    empty = "▫️" * (5 - rating)
    return f"{filled}{empty} ({rating})"


def format_datetime(dt: datetime) -> str:
    local = dt.astimezone(TZ)
    return f"{local.day} {MONTHS[local.month - 1]}, {local:%H:%M}"


def format_date(value) -> str:
    """Дата публикации приходит из базы объектом date или строкой."""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return value
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def short_address(address: str | None) -> str:
    """В карточке город не нужен — все филиалы в одном городе."""
    if not address:
        return "филиал не определён"
    parts = [p.strip() for p in address.split(",")]
    parts = [p for p in parts if p.lower() not in ("волгоград", "россия")]
    return ", ".join(parts) if parts else address


def render_card(card: CardData) -> str:
    """Текст карточки. Разметка HTML — её Telegram понимает без экранирования кавычек."""
    e = html.escape
    lines: list[str] = []

    head = STATUS_MARKS.get(card.status, STATUS_MARKS["new"])
    source = SOURCE_NAMES.get(card.source, card.source)
    # Негатив помечаем и после взятия в работу: отметка не должна исчезать,
    # пока отзыв не закрыт.
    if card.is_negative and card.status not in ("answered", "skipped"):
        head = f"🚨 {head.split(' ', 1)[1]}" if card.status != "attention" else head
    lines.append(f"<b>{head}</b> · {e(source)}")

    branch = short_address(card.branch_address)
    name = card.branch_name or "Филиал"
    lines.append(f"{e(name)}, {e(branch)}")
    lines.append("")

    lines.append(f"Оценка: {render_stars(card.rating)}")
    if card.rating is not None and not card.rating_confident:
        lines.append("⚠️ Оценка определена автоматически — проверьте")
    lines.append("")

    text = card.text_full or card.text
    if text:
        lines.append(f"<i>«{e(text)}»</i>")
    else:
        lines.append("<i>Отзыв без текста, только оценка</i>")
    lines.append("")

    if card.text_truncated and not card.text_full:
        by = SOURCE_BY.get(card.source, "площадкой")
        lines.append(f"⚠️ Текст обрезан {by} — нужен полный")
    if card.possible_duplicate:
        lines.append(f"⚠️ Возможный дубль отзыва #{card.duplicate_of}")
    if card.author:
        lines.append(f"👤 {e(card.author)}")

    lines.append(f"📅 Письмо: {format_datetime(card.received_at)}")
    if card.published_at:
        lines.append(f"   Опубликован: {format_date(card.published_at)}")
    elif card.source == "yandex":
        lines.append("   (отзыв опубликован на 1–2 дня раньше)")

    # Кто ведёт отзыв — видно всем, чтобы не взяли второй раз (п. 8 ТЗ).
    if card.assignee:
        who = ASSIGNEE_LABELS.get(card.status)
        if who:
            lines.append(f"🙋 {who}: {e(card.assignee)}")

    lines.append("")
    lines.append(f"<code>#{card.review_id}</code>")

    return "\n".join(lines)


def build_keyboard(card: CardData) -> list[list[tuple[str, str]]]:
    """
    Кнопки как список рядов: (подпись, callback_data или url).
    Возвращаем данные, а не объект aiogram, чтобы раскладку можно было
    проверить тестом без запуска бота.
    """
    rows: list[list[tuple[str, str]]] = []
    rid = card.review_id

    if card.status in OPEN_STATUSES:
        rows.append([("Взять в работу", f"rv:take:{rid}")])
    elif card.status == "in_progress":
        rows.append([
            ("Подготовить ответ", f"rv:draft:{rid}"),
            ("Отвечено", f"rv:done:{rid}"),
        ])
        rows.append([("Вернуть в очередь", f"rv:release:{rid}")])

    second: list[tuple[str, str]] = []
    if card.text_truncated and not card.text_full:
        second.append(("Добавить полный текст", f"rv:fulltext:{rid}"))
    if card.reply_url:
        where = SOURCE_SHORT.get(card.source, "кабинете")
        second.append((f"Открыть в {where}", f"url:{card.reply_url}"))
    if second:
        rows.append(second)

    if card.status in (*OPEN_STATUSES, "in_progress"):
        rows.append([("Без ответа", f"rv:skip:{rid}")])

    return rows


def render_keyboard_preview(card: CardData) -> str:
    """Текстовый вид клавиатуры — для тестов и для показа заказчику."""
    out = []
    for row in build_keyboard(card):
        out.append("  ".join(f"[ {label} ]" for label, _ in row))
    return "\n".join(out)
