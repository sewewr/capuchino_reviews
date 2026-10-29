"""
Проверка фильтров и поиска (п. 11 ТЗ) без Telegram.

    DATABASE_URL=postgresql://... python test_search.py
"""

import asyncio
import os
import re
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import search
import storage
from parsers.base import Review, make_dedup_key
from search import Filter
from telegram_ui import search_ui
from test_analytics import valid_html

TZ = search.TZ
fails = 0


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


def setup(conn):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS drafts, review_messages, reviews, emails, branches, state CASCADE")
    conn.commit()
    storage.init_schema(conn)
    from seed_branches import BRANCHES, SOURCES
    for source in SOURCES:
        for b in BRANCHES:
            storage.upsert_branch(conn, source, b["name"], b["address"])
    conn.commit()


NOW = datetime.now(TZ)


def at(days_ago, hour=12):
    d = NOW.date() - timedelta(days=days_ago)
    return datetime.combine(d, time(hour), TZ)


def add(conn, when, rating, text, source="yandex", address="Волгоград, улица Рокоссовского, 54",
        author=None, status=None, demo=False):
    r = Review(source=source, branch_name="Капучино", branch_address=address,
               rating=rating, rating_confident=True, text=text, author=author,
               received_at=when, warnings=["УЧЕБНЫЙ ОТЗЫВ"] if demo else [])
    r.dedup_key = make_dedup_key(r) + str(when)
    rid = storage.save_review(conn, r).review_id
    if status:
        storage.set_status(conn, rid, status)
    return rid


def ids(page):
    return {r.review_id for r in page.rows}


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


def main():
    conn = storage.connect(os.environ["DATABASE_URL"])
    setup(conn)

    r_today = add(conn, NOW - timedelta(minutes=5), 5, "Медовик бесподобный, ёлка в зале красивая",
                  author="Алёна")
    r_today_neg = add(conn, at(0, 0), 2, "Кофе холодный, ждали 40 минут", source="2gis",
                      address="Волгоград, Рокоссовского улица, 54", author="Иван")
    r_yest = add(conn, at(1), 4, "Хорошо, но шумно", address="Волгоград, переулок Ногина, 48",
                 status="answered")
    r_week = add(conn, at(5), 1, "Грубый кассир, 100% не вернусь", address="Волгоград, переулок Ногина, 48",
                 status="in_progress")
    r_old = add(conn, at(20), 5, "Торт «Алёнка» — лучший", source="2gis",
                address="Волгоград, Краснознаменская улица, 9", status="skipped")
    r_unknown = add(conn, at(2), 5, "Вкусно", source="2gis", address="Мира улица, 1")
    add(conn, at(0, 1), 1, "Учебный отзыв", demo=True)

    print("=== периоды ===")
    ok(ids(search.fetch(conn, Filter())) == {r_today, r_today_neg, r_yest, r_week, r_old, r_unknown},
       "«Все» — без учебного")
    ok(ids(search.fetch(conn, Filter(period="t"))) == {r_today, r_today_neg}, "«Сегодня» (и в 00:00 тоже)")
    ok(ids(search.fetch(conn, Filter(period="y"))) == {r_yest}, "«Вчера»")
    ok(ids(search.fetch(conn, Filter(period="w"))) == {r_today, r_today_neg, r_yest, r_week, r_unknown},
       "«Неделя» — 7 дней с сегодняшним")

    print("\n=== статусы ===")
    ok(ids(search.fetch(conn, Filter(status="n"))) == {r_today_neg, r_week}, "негативные")
    ok(ids(search.fetch(conn, Filter(status="o"))) == {r_today, r_today_neg, r_week, r_unknown},
       "необработанные — включая взятые в работу")
    ok(ids(search.fetch(conn, Filter(status="d"))) == {r_yest, r_old}, "обработанные — отвеченные и закрытые")

    print("\n=== филиал и площадка ===")
    choices = search.branch_choices(conn)
    ok(len(choices) == 10, f"филиалов в выборе: {len(choices)} — по одному на адрес")
    rokoss = next(k for k, name in choices if "Рокоссовского" in name)
    ok(ids(search.fetch(conn, Filter(branch=rokoss))) == {r_today, r_today_neg},
       "филиал — отзывы обеих площадок")
    ok(ids(search.fetch(conn, Filter(branch="u"))) == {r_unknown}, "«не определён»")
    ok(ids(search.fetch(conn, Filter(source="g"))) == {r_today_neg, r_old, r_unknown}, "только 2ГИС")
    ok(ids(search.fetch(conn, Filter(period="w", status="n", source="y"))) == {r_week},
       "сочетание: неделя + негатив + Яндекс")

    print("\n=== поиск ===")
    def find(text, **kw):
        return ids(search.fetch(conn, Filter(query=search.save_query(conn, text), **kw)))
    ok(find("МЕДОВИК") == {r_today}, "без учёта регистра")
    ok(find("елка") == {r_today}, "ё и е — одно и то же")
    ok(find("алёнка") == {r_old}, "в тексте «Алёнка»")
    ok(find("иван") == {r_today_neg}, "по имени гостя")
    ok(find("100%") == {r_week}, "знак % ищется как символ, а не шаблон")
    ok(find("_") == set(), "подчёркивание — тоже как символ")
    ok(find("кофе", status="d") == set(), "поиск сочетается с фильтрами")
    ok(find("несуществующее") == set(), "ничего не нашлось")

    print("\n=== кнопки и данные в них ===")
    longest = Filter("w", "n", "g", "999999", 99, "a" * 10)
    ok(len(longest.encode().encode()) <= 64, f"самая длинная кнопка: {len(longest.encode().encode())} байт из 64")
    ok(Filter.decode(longest.encode()) == longest, "фильтр восстанавливается из кнопки")
    ok(Filter.decode("fl:мусор") == Filter(), "кривые данные — фильтр по умолчанию, без падения")
    ok(Filter.decode("fl:xyz:1;DROP:0:") == Filter(), "чужие значения отвергаются")
    ok(Filter(page=3).with_(status="n").page == 0, "смена фильтра — на первую страницу")

    print("\n=== страницы ===")
    for i in range(20):
        add(conn, at(3, 1) + timedelta(minutes=i), 5, f"Отзыв номер {i}")
    p1 = search.fetch(conn, Filter())
    p3 = search.fetch(conn, Filter(page=3))
    ok(p1.total == 26 and p1.pages == 4 and len(p1.rows) == search.PAGE_SIZE,
       f"всего {p1.total}, страниц {p1.pages}")
    ok(len(p3.rows) == 26 - 3 * search.PAGE_SIZE, "последняя страница — остаток")
    gone = search.fetch(conn, Filter(page=9))
    ok(gone.filter.page == 0 and gone.rows, "страницы больше нет — показываем первую")
    ok(p1.rows[0].review_id == r_today, "свежие сверху")

    print("\n=== как выглядит ===")
    key = search.save_query(conn, "алёнка")
    page = search.fetch(conn, Filter(query=key))
    text = search.render(page, conn)
    ok(valid_html(text), "HTML корректный")
    ok("<b>Алёнка</b>" in text, "найденное слово выделено")
    page = search.fetch(conn, Filter(period="w", status="o", branch=rokoss))
    text = search.render(page, conn)
    print("\n" + "\n".join("    " + l for l in plain(text).split("\n")) + "\n")
    ok(valid_html(text), "HTML корректный")
    ok("за неделю · необработанные · ул. Рокоссовского, 54" in text, "заголовок называет фильтры")
    ok("Поиск устарел" in search.render(search.fetch(conn, Filter(query="nokey")), conn),
       "неизвестный ключ поиска — понятное сообщение")
    long_text = "а" * 200 + " искомое " + "б" * 200
    snip = search.snippet(long_text, "искомое")
    ok(snip.startswith("…") and snip.endswith("…") and "<b>искомое</b>" in snip,
       "длинный текст — кусок вокруг найденного")
    ok(search.snippet("<script>", None) == "&lt;script&gt;", "текст экранирован")

    print("\n=== кнопки в Telegram ===")
    asyncio.run(telegram(conn, rokoss, r_today))

    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


class Msg:
    def __init__(self, text=""):
        self.chat = SimpleNamespace(id=-1001111)
        self.text = text
        self.out, self.edited, self.markup = [], None, None

    async def answer(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))

    async def edit_text(self, text, **kw):
        self.edited, self.markup = text, kw.get("reply_markup")

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.markup = reply_markup


class FakeState:
    def __init__(self):
        self.state, self.data = None, {}

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state, self.data = None, {}


def labels(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


def press(markup, label_part):
    for row in markup.inline_keyboard:
        for b in row:
            if label_part in b.text:
                return b.callback_data
    raise KeyError(label_part)


async def telegram(conn, rokoss, first_id):
    m = Msg()
    await search_ui.cmd_reviews(m, conn)
    text, kb = m.out[0]
    print("    " + "\n    ".join(" | ".join(r) for r in labels(kb)))
    ok("✖ Сбросить" not in str(labels(kb)), "без фильтров кнопки «Сбросить» нет")
    ok(len(kb.inline_keyboard[0]) == 4 and kb.inline_keyboard[0][0].text == "✓ Все", "периоды, «Все» отмечено")

    async def click(data):
        cb = SimpleNamespace(data=data, message=Msg(), bot=None, answers=[])

        async def answer(text="", **kw):
            cb.answers.append(text)
        cb.answer = answer
        return cb

    cb = await click(press(kb, "Негативные"))
    await search_ui.on_filter(cb, conn)
    ok("✓ 🚨 Негативные" in str(labels(cb.message.markup)), "нажали «Негативные» — отмечено")
    ok("✖ Сбросить" in str(labels(cb.message.markup)), "появилась «Сбросить»")
    again = await click(press(cb.message.markup, "Негативные"))
    await search_ui.on_filter(again, conn)
    ok("✓ 🚨" not in str(labels(again.message.markup)), "повторное нажатие снимает фильтр")

    cb = await click(press(kb, "Все филиалы"))
    await search_ui.on_branch_picker(cb, conn)
    picker = labels(cb.message.markup)
    ok(sum(len(r) for r in picker) == 13, "выбор филиала: 10 + не определён + все + назад")
    cb2 = await click(press(cb.message.markup, "Рокоссовского"))
    await search_ui.on_filter(cb2, conn)
    ok("🏠 ул. Рокоссовского, 54 ▾" in str(labels(cb2.message.markup)), "выбранный филиал на кнопке")

    cb = await click(press(kb, "Все площадки"))
    await search_ui.on_filter(cb, conn)
    ok("📍 Яндекс" in str(labels(cb.message.markup)), "площадка переключается по кругу: Яндекс")

    st = FakeState()
    cb = await click(press(cb2.message.markup, "Поиск"))
    await search_ui.on_search_button(cb, st)
    ok(st.state is not None and "сохранятся" in cb.message.out[0][0], "поиск с сохранением фильтров")
    m = Msg("кофе")
    await search_ui.receive_search(m, st, conn)
    ok("ул. Рокоссовского, 54 · поиск «кофе»" in m.out[0][0] and "Найдено: 1" in m.out[0][0],
       "поиск внутри выбранного филиала")

    m = Msg()
    await search_ui.cmd_find(m, FakeState(), conn, SimpleNamespace(args="а"))
    ok("Слишком коротко" in m.out[0][0], "одна буква — просим больше")

    from telegram_ui import bot as B
    posted = []

    async def fake_post(bot, conn_, chat_id, review_id):
        posted.append((chat_id, review_id))
    original, B.post_card = B.post_card, fake_post
    try:
        cb = await click(press(kb, f"#{first_id}"))
        await search_ui.on_open(cb, conn)
    finally:
        B.post_card = original
    ok(posted == [(-1001111, first_id)], "номер отзыва присылает карточку с кнопками")


if __name__ == "__main__":
    main()
