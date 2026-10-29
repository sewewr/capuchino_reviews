"""
Проверка аналитики (п. 12 ТЗ) и еженедельного отчёта (п. 13 ТЗ) без Telegram.

    DATABASE_URL=postgresql://... python test_analytics.py
"""

import asyncio
import os
import re
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

import analytics as A
import storage
from parsers.base import Review, make_dedup_key
from telegram_ui import stats_ui

TZ = A.TZ
NOW = datetime(2026, 10, 26, 10, 5, tzinfo=TZ)  # понедельник, 10:05
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


ROKOSS_Y = "Волгоград, улица Рокоссовского, 54"
ROKOSS_G = "Волгоград, Рокоссовского улица, 54"
NOGINA = "Волгоград, переулок Ногина, 48"


def add(conn, when, rating, text, source="yandex", address=ROKOSS_Y, status=None,
        demo=False, author=None):
    r = Review(source=source, branch_name="Капучино", branch_address=address,
               rating=rating, rating_confident=True, text=text, author=author,
               received_at=when, warnings=["УЧЕБНЫЙ ОТЗЫВ"] if demo else [])
    r.dedup_key = make_dedup_key(r)
    rid = storage.save_review(conn, r).review_id
    if status:
        storage.set_status(conn, rid, status)
    return rid


def at(days_ago, hour=12):
    d = NOW.date() - timedelta(days=days_ago)
    return datetime.combine(d, time(hour), TZ)


class TagCheck(HTMLParser):
    """Telegram отвергает сообщение с незакрытым тегом — проверяем заранее."""

    ALLOWED = {"b", "i", "code", "a", "pre"}

    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            self.bad.append(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append(f"/{tag}")


def valid_html(text):
    p = TagCheck()
    p.feed(text)
    return not p.bad and not p.stack


def plain(text):
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">")


def seed(conn):
    # Прошлая неделя (19–25 октября): 6 настоящих отзывов
    add(conn, at(7), 5, "Медовик бесподобный", status="answered")
    add(conn, at(6), 5, "Всё вкусно", source="2gis", address=ROKOSS_G, author="Ольга",
        status="answered")
    add(conn, at(5), 4, "Хорошо, но шумно", address=NOGINA, status="answered")
    add(conn, at(3), 3, "Капучино <так себе> & десерт ок", address=NOGINA, status="in_progress")
    add(conn, at(2), 1, "Ждали заказ 40 минут, кофе холодный")      # негатив, не взят
    add(conn, at(1), 2, "Торт был несвежий", source="2gis", address=ROKOSS_G, author="Иван")
    # дубль отзыва выше — в статистику не идёт
    add(conn, at(1, 15), 2, "Торт был несвежий", source="2gis", address=ROKOSS_G, author="Иван")
    # учебный — не идёт
    add(conn, at(2), 1, "Учебный негатив", demo=True)
    # позапрошлая неделя (12–18 октября): 3 отзыва
    add(conn, at(10), 5, "Отлично", status="answered")
    add(conn, at(12), 5, "Супер", status="answered")
    add(conn, at(13), 3, "Нормально", address=NOGINA, status="skipped")
    # сегодня, понедельник — в отчёт за прошлую неделю не входит
    add(conn, at(0, 9), 5, "Утренний кофе отличный")
    # старый негатив без ответа — месяц назад
    add(conn, at(30), 2, "Грубый кассир", address=NOGINA)


def unit():
    print("=== периоды ===")
    p = A.period_report(NOW)
    ok((p.start, p.end) == (date(2026, 10, 19), date(2026, 10, 26)),
       f"отчёт в понедельник — прошлая неделя пн–вс: {p.label()}")
    ok(p.previous().start == date(2026, 10, 12), "сравнение — с неделей раньше")
    ok(A.period_week(NOW).label() == "20–26 октября", "«Неделя» — 7 дней с сегодняшним")
    ok(A.period_today(NOW).label() == "26 октября", "«Сегодня»")
    ok(A.period_month(NOW).label() == "27 сентября – 26 октября", "«Месяц» — 30 дней")

    print("\n=== свой период ===")
    for raw, expect in [("01.10 15.10", (date(2026, 10, 1), date(2026, 10, 16))),
                        ("1.10.2026–15.10.2026", (date(2026, 10, 1), date(2026, 10, 16))),
                        ("15.10 01.10", (date(2026, 10, 1), date(2026, 10, 16))),
                        ("05.10", (date(2026, 10, 5), date(2026, 10, 6))),
                        ("20.12", (date(2025, 12, 20), date(2025, 12, 21)))]:
        got = A.parse_period(raw, NOW)
        ok((got.start, got.end) == expect, f"«{raw}» → {got.label()}")
    for bad in ("вчера", "31.02", "01.01.2024 01.01.2026"):
        try:
            A.parse_period(bad, NOW)
            ok(False, f"«{bad}» должно отказать")
        except ValueError as e:
            ok(True, f"«{bad}» → {e}")

    print("\n=== расписание ===")
    for raw, expect in [("пн 10:00", (0, time(10), True)), ("пятница 18:30", (4, time(18, 30), True)),
                        ("9:00", (None, time(9), True)), ("ср", (2, None, True)),
                        ("вс 9", (6, time(9), True)), ("выкл", (None, None, False))]:
        ok(A.parse_schedule(raw) == expect, f"«{raw}» → {A.parse_schedule(raw)}")
    try:
        A.parse_schedule("25:00")
        ok(False, "25:00 должно отказать")
    except ValueError:
        ok(True, "«25:00» — понятная ошибка")
    s = A.Schedule(True, 4, time(18), "-1")
    ok(s.describe() == "каждую пятницу в 18:00 (МСК)", s.describe())

    sched = A.Schedule(True, 0, time(10), "-100123")
    ok(A.report_due(sched, None, NOW), "понедельник 10:05 — пора")
    ok(not A.report_due(sched, A.week_key(NOW), NOW), "уже отправлен на этой неделе — нет")
    ok(not A.report_due(sched, None, NOW.replace(hour=9)), "9:00 — ещё рано")
    ok(A.report_due(sched, None, NOW.replace(hour=15)), "бот лежал утром — догоняет в тот же день")
    ok(not A.report_due(sched, None, NOW + timedelta(days=1)), "во вторник устаревший не шлём")
    ok(not A.report_due(A.Schedule(False, 0, time(10), "-1"), None, NOW), "выключен — нет")
    ok(not A.report_due(A.Schedule(True, 0, time(10), None), None, NOW), "без чата — нет")

    print("\n=== мелочи оформления ===")
    ok([A.reviews_word(n) for n in (1, 2, 5, 11, 21, 104)] ==
       ["отзыв", "отзыва", "отзывов", "отзывов", "отзыв", "отзыва"], "склонение")
    ok(A.num(4.66) == "4,7", "дробь через запятую")
    ok(A.message_link("-1001234567", 55) == "https://t.me/c/1234567/55", "ссылка на карточку")
    ok(A.message_link("123456", 55) is None, "в личке ссылки нет")
    ok(A.short_address("бул. 30-летия Победы, 21, район Семь Ветров, Волгоград")
       == "бул. 30-летия Победы, 21", "район и город убраны")


class FakeBot:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("chat not found")
        self.sent.append((str(chat_id), text))
        return SimpleNamespace(message_id=len(self.sent))


def with_db(conn):
    setup(conn)
    seed(conn)
    # карточка негатива лежит в общем чате — в отчёте будет ссылка
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reviews WHERE text LIKE 'Ждали%'")
        rid = cur.fetchone()["id"]
    storage.remember_message(conn, rid, "-1009876543", 777)
    storage.remember_message(conn, rid, "-1005555555", 12)

    print("\n=== подсчёт за прошлую неделю ===")
    s = A.collect(conn, A.period_report(NOW), storage.negative_threshold(conn))
    ok(s.total == 6, f"отзывов: {s.total} (учебный и дубль не считаются)")
    ok(A.num(s.avg) == "3,3", f"средняя: {A.num(s.avg)}  (20/6)")
    ok(s.stars == {1: 1, 2: 1, 3: 1, 4: 1, 5: 2}, f"по звёздам: {s.stars}")
    ok(s.negative == 3, f"негативных при пороге 3: {s.negative}")
    ok(s.open == 3 and s.in_progress == 1, f"без ответа: {s.open}, в работе: {s.in_progress}")
    ok(s.by_source == {"yandex": (4, 3.25), "2gis": (2, 3.5)}, f"площадки: {s.by_source}")
    names = [(b.name, b.total) for b in s.by_branch]
    ok(names == [("ул. Рокоссовского, 54", 4), ("пер. Ногина, 48", 2)],
       f"филиал склеен по обеим площадкам: {names}")

    print("\n=== /stats ===")
    text = A.render_stats(s, A.collect(conn, A.period_report(NOW).previous()))
    ok(valid_html(text), "HTML корректный — Telegram примет")
    print("\n" + "\n".join("    " + l for l in plain(text).split("\n")) + "\n")
    ok("▲ +3 к предыдущему периоду" in plain(text), "динамика к прошлому периоду")
    empty = A.render_stats(A.collect(conn, A.parse_period("01.01.2026", NOW)))
    ok("Отзывов за этот период нет" in empty, "пустой период — без таблицы нулей")

    print("=== еженедельный отчёт ===")
    report = A.build_report(conn, now=NOW, chat="-1009876543")
    ok(valid_html(report), "HTML корректный")
    print("\n" + "\n".join("    " + l for l in plain(report).split("\n")) + "\n")
    ok("Отзывов: 6 (▲ +3 к прошлой неделе)" in plain(report), "количество и динамика")
    ok("👍 4–5★: 3 · 😐 3★: 1 · 👎 1–2★: 2" in plain(report), "позитив / нейтральные / негатив")
    ok("🏆 Больше всего отзывов: ул. Рокоссовского, 54 — 4" in plain(report), "филиал-лидер")
    ok("Требуют внимания: 4" in plain(report),
       "негатив без ответа за всё время: старый, новые и взятый в работу 3★")
    ok(plain(report).index("Грубый кассир") < plain(report).index("Ждали заказ"),
       "дольше всех ждущие — первыми")
    ok('href="https://t.me/c/9876543/777"' in report, "ссылка ведёт на карточку в чате отчёта")
    ok("&lt;так себе&gt; &amp; десерт" in report, "текст отзыва экранирован")
    ok("Утренний кофе" not in report, "сегодняшний отзыв в отчёт за прошлую неделю не попал")
    ok("Учебный" not in report, "учебный не попал")

    print("\n=== отправка по расписанию ===")
    storage.set_state(conn, A.REPORT_CHAT, "-1009876543")
    fb = FakeBot()
    sent1 = asyncio.run(stats_ui.send_if_due(fb, conn, now=NOW))
    sent2 = asyncio.run(stats_ui.send_if_due(fb, conn, now=NOW + timedelta(minutes=1)))
    ok(sent1 and not sent2 and len(fb.sent) == 1, "ушёл один раз, через минуту не повторился")
    ok(fb.sent[0][0] == "-1009876543", "в чат из /report_here")

    storage.set_state(conn, A.REPORT_DAY, "4")
    storage.set_state(conn, A.REPORT_TIME, "18:00")
    friday = datetime(2026, 11, 6, 18, 0, tzinfo=TZ)  # следующая неделя
    alerts = []

    async def alert(text):
        alerts.append(text)

    broken = FakeBot(fail=True)
    asyncio.run(stats_ui.send_if_due(broken, conn, alert, now=friday))
    again = asyncio.run(stats_ui.send_if_due(broken, conn, alert, now=friday + timedelta(minutes=1)))
    ok(len(alerts) == 1 and not again, "чат недоступен — одно предупреждение, без спама")

    print("\n=== соединение живо после ошибки в запросе ===")
    try:
        A._reading(conn, lambda: conn.execute("SELECT nonexistent FROM reviews"))
    except Exception:
        pass
    ok("Отзывов" in A.build_stats(conn, A.period_report(NOW)), "следующий запрос работает")


def main():
    unit()
    conn = storage.connect(os.environ["DATABASE_URL"])
    with_db(conn)
    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


if __name__ == "__main__":
    main()
