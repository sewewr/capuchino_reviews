"""
Проверка: реклама под видом отзыва не мешает работе.

    DATABASE_URL=postgresql://... python test_spam.py
"""

import asyncio
import os
import re
from datetime import datetime, timedelta

import analytics
import search
import spam
import storage
from parsers.base import Review, make_dedup_key
from telegram_ui import bot as B
from telegram_ui.cards import TZ, build_keyboard, render_card
from test_assignees import FakeBot, FakeCallback, setup

fails = 0


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


AD = ("Помогаем бизнесу усиливать репутацию в Яндекс и 2ГИС. 2ГИС — 50 отзывов: "
      "3 700 ₽, Яндекс — 30 отзывов: 4 500 ₽. Пишите @otzyv_pro")

TEXTS = [
    (AD, True),
    ("Помогаем бизнесу усиливать репутацию в Яндекс и 2ГИС. 2ГИС — 50 отзывов: 3 700 ₽...", True),
    ("Накрутка отзывов недорого, telegram @reviews_top", True),
    ("Выведем вашу карточку в топ-3 Яндекс Карт, бесплатный аудит", True),
    ("Отзывы на Яндекс Картах от 150 ₽ за штуку, пишите", True),
    ("Пишем отзывы, 200 ₽ за отзыв, Яндекс и 2ГИС", True),
    # настоящие отзывы — не трогать
    ("Очень вкусный торт Алёнка! Взяла по рекомендации! Осталась довольна", False),
    ("Торт за 1500 ₽ очень вкусный, нашла вас в 2ГИС", False),
    ("Читала отзывы в Яндексе, 100 отзывов не врут — всё вкусно, кофе 250 руб", False),
    ("Кафе с хорошей репутацией в Яндексе, но кофе холодный, 250 руб", False),
    ("Для бизнес-ланча отличное место, обед 450 ₽, недорого, пишите в 2гис", False),
    ("Капучино 200 руб за штуку, в 2ГИС цена другая, пишите", False),
    ("Акция 2+1 на капучино, недорого, нашла в 2ГИС", False),
    ("Ужасно! Ждали 40 минут, пишите жалобу в Роспотребнадзор", False),
    ("", False),
]


def add(conn, rating, text, hours_ago=1):
    r = Review(source="yandex", branch_name="Капучино",
               branch_address="Волгоград, улица Рокоссовского, 54",
               rating=rating, rating_confident=True, text=text,
               received_at=datetime.now(TZ) - timedelta(hours=hours_ago))
    r.dedup_key = make_dedup_key(r)
    return storage.save_review(conn, r)


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


async def scenario(conn):
    os.environ["TELEGRAM_ADMIN_CHAT"] = "-100ADMIN"
    os.environ["REVIEWS_CHAT_ID"] = "-100ALL"
    fb = FakeBot()

    print("\n=== реклама пришла с почтой ===")
    ad = add(conn, 5, AD)
    real = add(conn, 5, "Очень вкусный торт Алёнка!")
    ok(ad.is_spam and not real.is_spam, "реклама распознана, настоящий отзыв — нет")
    await B.deliver(fb, conn, "-100ALL", ad.review_id, "-100NEG")
    await B.deliver(fb, conn, "-100ALL", real.review_id, "-100NEG")
    chats = sorted(c for c, _ in fb.messages)
    ok(chats == ["-100ADMIN", "-100ALL"], f"реклама ушла только администратору: {chats}")
    card = B.card_for(conn, ad.review_id)
    text = plain(render_card(card))
    print("\n" + "\n".join("    " + l for l in text.split("\n")))
    ok("🚫 РЕКЛАМА" in text and "автоматически" in text, "карточка помечена")
    ok([l for r in build_keyboard(card) for l, _ in r] == ["↩️ Это не реклама"],
       "на рекламе одна кнопка — вернуть")

    print("\n=== не попадает в статистику, отчёт и списки ===")
    st = analytics.collect(conn, analytics.period_week())
    ok(st.total == 1, f"в /stats отзывов: {st.total} (реклама не считается)")
    page = search.fetch(conn, search.Filter())
    ok({r.review_id for r in page.rows} == {real.review_id}, "в /reviews рекламы нет")
    spam_page = search.fetch(conn, search.Filter(status="s"))
    ok({r.review_id for r in spam_page.rows} == {ad.review_id}, "в /spam — она")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM reviews WHERE status IN ('new','attention')")
        ok(cur.fetchone()["n"] == 1, "в /new рекламы нет")

    print("\n=== ошибся фильтр: «Это не реклама» ===")
    admin_copy = next(mid for (c, mid) in fb.messages if c == "-100ADMIN")
    cb = FakeCallback(fb, f"rv:notspam:{ad.review_id}", "-100ADMIN", admin_copy, 1, "Наталья")
    await B.on_button(cb, None, conn)
    card = B.card_for(conn, ad.review_id)
    ok(card.status == "new", f"статус вернулся: {card.status}")
    with conn.cursor() as cur:
        cur.execute("SELECT warnings FROM reviews WHERE id = %s", (ad.review_id,))
        warns = cur.fetchone()["warnings"] or []
    ok(not any("рекламу" in w for w in warns), f"пометка снята: {warns}")
    ok(any(c == "-100ALL" for c, _ in storage.review_messages(conn, ad.review_id)),
       "карточка ушла в общий чат")
    ok(analytics.collect(conn, analytics.period_week()).total == 2, "теперь считается в статистике")

    print("\n=== фильтр пропустил: «🚫 Реклама» на карточке ===")
    sneaky = add(conn, 5, "Сделаем вам красивые фото еды, пишите в личку").review_id
    card = B.card_for(conn, sneaky)
    ok(("🚫 Реклама", f"rv:spam:{sneaky}") in [b for r in build_keyboard(card) for b in r],
       "кнопка есть на новой карточке")
    await B.post_card(fb, conn, "-100ALL", sneaky)
    await B.post_card(fb, conn, "-100NEG", sneaky)
    copy = next(mid for (c, mid) in sorted(fb.messages, reverse=True) if c == "-100ALL")
    cb = FakeCallback(fb, f"rv:spam:{sneaky}", "-100ALL", copy, 2, "Анна")
    await B.on_button(cb, None, conn)
    ok("Скрыто как реклама" in cb.alerts[0][0], f"ответ: {cb.alerts[0][0]}")
    copies = [plain(fb.messages[(c, m)]) for c, m in storage.review_messages(conn, sneaky)]
    ok(len(copies) == 2 and all("🚫 РЕКЛАМА" in t and "Пометил(а) как рекламу: Анна" in t
                                for t in copies), "обе копии перерисованы, видно, кто пометил")


def main():
    print("=== распознавание ===")
    for text, expected in TEXTS:
        got, signs = spam.detect(text)
        ok(got == expected, f"{'реклама ' if expected else 'отзыв   '} «{text[:55]}»"
           + (f"  [{', '.join(signs)}]" if signs else ""))

    url = os.environ["DATABASE_URL"]
    conn = storage.connect(url)
    setup(conn)
    asyncio.run(scenario(conn))

    print("\n=== реклама, пришедшая до фильтра ===")
    with conn.cursor() as cur:
        cur.execute("UPDATE reviews SET status = 'new' WHERE status = 'spam'")
        cur.execute("INSERT INTO reviews (source, rating, text, received_at, dedup_key, status) "
                    "VALUES ('2gis', 5, %s, now(), 'old-ad', 'answered')", (AD,))
    conn.commit()
    hidden = storage.hide_existing_spam(conn)
    ok(len(hidden) == 1, f"скрыто: {len(hidden)} — рекламный; «фото еды» правило не ловит, "
       "его помечают кнопкой")
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM reviews WHERE dedup_key = 'old-ad'")
        ok(cur.fetchone()["status"] == "answered", "закрытые человеком не трогаем")
    ok(storage.hide_existing_spam(conn) == [], "повторный запуск ничего не меняет")
    conn.close()

    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


if __name__ == "__main__":
    main()
