"""
Как будут выглядеть карточки отзывов в Telegram.
Берём отзывы из базы и рисуем их текстом, без запуска бота.

    DATABASE_URL=postgresql://... python preview_cards.py
"""
import os, re, storage
from telegram_ui.cards import CardData, render_card, render_keyboard_preview


def show(row: dict, title: str) -> None:
    card = CardData(**row)
    text = render_card(card)
    text = re.sub(r"</?(b|i|code)>", "", text)   # убираем разметку для чтения
    print("\n" + "─" * 46)
    print(f"  {title}")
    print("─" * 46)
    print(text)
    print(render_keyboard_preview(card))


conn = storage.connect(os.environ["DATABASE_URL"])
with conn.cursor() as cur:
    cur.execute("SELECT id FROM reviews ORDER BY id")
    ids = [r["id"] for r in cur.fetchall()]

for rid in ids:
    row = storage.get_card_data(conn, rid)
    show(dict(row), f"отзыв #{rid}, статус «{row['status']}»")

# те же карточки в других состояниях
if ids:
    base = dict(storage.get_card_data(conn, ids[0]))

    variant = dict(base); variant["status"] = "in_progress"
    show(variant, "тот же отзыв, взят в работу")

    variant = dict(base)
    variant["rating"] = 2; variant["rating_confident"] = False
    variant["text"] = "Ждали заказ 40 минут, кофе принесли холодный."
    variant["text_truncated"] = False
    show(variant, "негативный отзыв, оценка под вопросом")

    variant = dict(base)
    variant["text_full"] = "Полный текст отзыва, добавленный сотрудником вручную после перехода в кабинет."
    variant["author"] = "Мария К."
    variant["published_at"] = "2026-08-26"
    variant["status"] = "answered"
    show(variant, "текст добран, отзыв закрыт")

conn.close()
