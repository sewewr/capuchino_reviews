"""
Проверка третьего этапа: сравнение филиалов, динамика, ежемесячный отчёт.
GigaChat не нужен — подставляется поддельная модель.

    DATABASE_URL=postgresql://... python test_management.py
"""

import asyncio
import os
import re
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import analytics
import management as M
import storage
import topics
from parsers.base import Review, make_dedup_key
from test_analytics import TZ, valid_html
from test_search import setup
from test_topics import FakeModel

fails = 0
NOW = datetime(2026, 11, 1, 10, 30, tzinfo=TZ)  # 1 ноября, воскресенье


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


ROK = "Волгоград, улица Рокоссовского, 54"
NOG = "Волгоград, переулок Ногина, 48"
KRA = "Волгоград, Краснознаменская улица, 9"


def add(conn, day: datetime, rating, text, address=ROK, source="yandex", closed_hours=None):
    r = Review(source=source, branch_name="Капучино", branch_address=address, rating=rating,
               rating_confident=True, text=text, received_at=day)
    r.dedup_key = make_dedup_key(r) + str(day)
    rid = storage.save_review(conn, r).review_id
    if closed_hours is not None:
        with conn.cursor() as cur:
            cur.execute("UPDATE reviews SET status = 'answered', closed_at = %s WHERE id = %s",
                        (day + timedelta(hours=closed_hours), rid))
        conn.commit()
    return rid


def d(y, m, day, h=12):
    return datetime(y, m, day, h, tzinfo=TZ)


def seed(conn):
    # сентябрь
    add(conn, d(2026, 9, 5), 5, "Медовик бесподобный", ROK, closed_hours=3)
    add(conn, d(2026, 9, 12), 4, "Торт хороший", NOG, closed_hours=10)
    add(conn, d(2026, 9, 20), 5, "Уютно", KRA)
    # октябрь
    add(conn, d(2026, 10, 2), 5, "Торт Алёнка — любовь", ROK, closed_hours=2)
    add(conn, d(2026, 10, 9), 5, "Медовик, кассир приветливый", ROK, closed_hours=4)
    add(conn, d(2026, 10, 15), 4, "Уютно, торт вкусный", ROK, source="2gis", closed_hours=6)
    add(conn, d(2026, 10, 20), 2, "Ждали 40 минут, кофе холодный", NOG, closed_hours=30)
    add(conn, d(2026, 10, 25), 1, "Ждали заказ полчаса, кассир грубый", NOG)
    add(conn, d(2026, 10, 28), 3, "Торт был несвежий", NOG, closed_hours=50)
    add(conn, d(2026, 10, 30), 5, "Помогаем бизнесу усиливать репутацию в Яндекс и 2ГИС. "
                                   "2ГИС — 50 отзывов: 3 700 ₽", KRA)  # реклама — не считается
    # ноябрь — не входит в отчёт за октябрь
    add(conn, d(2026, 11, 1, 9), 5, "Утренний кофе")


def main():
    print("=== календарные месяцы ===")
    p = M.last_full_month(NOW)
    ok((p.start.isoformat(), p.end.isoformat()) == ("2026-10-01", "2026-11-01"), f"прошлый месяц: {p.title}")
    ok(M.previous_month(p).title == "Сентябрь 2026", "месяц до него")
    ok(M.previous_month(M.month_period(2026, 1)).title == "Декабрь 2025", "через новый год")
    ok(M.this_month_so_far(NOW).label() == "1 ноября", "текущий месяц по сегодня")

    conn = storage.connect(os.environ["DATABASE_URL"])
    setup(conn)
    seed(conn)
    topics.process_pending(conn, FakeModel())

    print("\n=== сравнение филиалов за октябрь ===")
    c = M.compare_branches(conn, p, M.previous_month(p))
    rows = {r.name: r for r in c.rows}
    rok, nog = rows["ул. Рокоссовского, 54"], rows["пер. Ногина, 48"]
    ok(len(c.rows) == 10, f"все 10 филиалов в сравнении, даже без отзывов: {len(c.rows)}")
    ok(c.rows[0].name == "ул. Рокоссовского, 54" and c.rows[1].name == "пер. Ногина, 48",
       "порядок — по средней оценке, пустые в конце")
    ok(rok.total == 3 and M.num(rok.avg) == "4,7", f"Рокоссовского: {rok.total} отз., {M.num(rok.avg)}★ (обе площадки)")
    ok(nog.negative == 3 and M.num(nog.negative_share * 100) == "100,0", "Ногина: весь негатив")
    ok(round(nog.closed_share, 2) == 0.67, f"Ногина: закрыто {nog.closed_share:.0%}")
    ok(rok.reaction_hours == 4 and nog.reaction_hours == 40, f"медиана реакции: {rok.reaction_hours} ч и {nog.reaction_hours} ч")
    ok(nog.top_complaint == "waiting", f"главная жалоба Ногина: {nog.top_complaint}")
    ok(M.num(rok.prev_avg) == "5,0" and M.num(nog.prev_avg) == "4,0", "средняя прошлого месяца по филиалам")
    ok(c.network.total == 6, f"сеть: {c.network.total} (реклама и ноябрь не считаются)")

    text = M.render_comparison(c)
    ok(valid_html(text), "HTML корректный")
    print("\n" + "\n".join("    " + re.sub("<[^>]+>", "", l) for l in text.split("\n")) + "\n")
    ok("⏱ 40 ч" in text and "жалобы: время ожидания" in text, "время реакции и жалобы в строке филиала")
    ok("Без отзывов:" in text, "филиалы без отзывов перечислены отдельно")

    print("=== динамика ===")
    s = M.dynamics(conn, "month", now=NOW, count=3)
    ok([p.label for p in s.points] == ["сен", "окт", "ноя"], f"месяцы: {[p.label for p in s.points]}")
    ok([p.total for p in s.points] == [3, 6, 1], f"отзывов: {[p.total for p in s.points]}")
    w = M.dynamics(conn, "week", now=NOW)
    ok(len(w.points) == 12 and sum(p.total for p in w.points) == 10, "12 недель, все отзывы на месте")
    nog_id = next(k for k, n in __import__("search").branch_choices(conn) if "Ногина" in n)
    sb = M.dynamics(conn, "month", branch=nog_id, now=NOW, count=3)
    ok(sb.title == "пер. Ногина, 48" and [p.total for p in sb.points] == [1, 3, 0], "динамика одного филиала")
    trend = M.describe_trend(s, "month")
    ok(valid_html(trend) and "снизилась" in trend, f"вывод: {re.sub('<[^>]+>', '', trend).splitlines()[2]}")
    png = M.chart_png(w, "week")
    ok(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10000, f"график PNG, {len(png) // 1024} КБ")
    open("/tmp/dynamics_week.png", "wb").write(png)
    open("/tmp/dynamics_month.png", "wb").write(M.chart_png(s, "month"))
    empty = M.describe_trend(M.Series("Вся сеть", [M.Point("01.01", NOW.date(), 0, None)]), "week")
    ok("нужно хотя бы два" in empty, "мало данных — честно говорим")

    print("\n=== ежемесячный управленческий отчёт ===")
    model = FakeModel()
    rep = M.build_monthly(conn, p, NOW, ai_client=model)
    for part in (rep.text, rep.comparison, rep.topics_text, rep.chart_caption):
        ok(valid_html(part) and len(part) < 4000, f"часть отчёта влезает в сообщение ({len(part)} зн.)")
    print("\n" + "\n".join("    " + re.sub("<[^>]+>", "", l) for l in rep.text.split("\n")) + "\n")
    ok("Отзывов: <b>6</b> (▲3)" in rep.text, "количество и рост к сентябрю")
    ok("Выводы и рекомендации" in rep.text, "выводы ИИ")
    sent = [u for kind, u in model.calls if kind == "summary"]
    ok(sent and "пер. Ногина, 48: 2,0" in sent[-1] and "Время ожидания" in sent[-1],
       "модели переданы филиалы и жалобы")
    ok("Причины негатива" in rep.topics_text, "блок причин негатива")
    ok(rep.chart is not None, "график динамики по месяцам")
    M.build_monthly(conn, p, NOW, ai_client=model)
    ok(sum(1 for k, _ in model.calls if k == "summary") == 1, "повторно — из кэша, без запроса к ИИ")

    class Down:
        def chat(self, chat):
            raise ConnectionError("нет связи")
    rep2 = M.build_monthly(conn, M.month_period(2026, 9), NOW, ai_client=Down())
    ok("Выводы ИИ не получились" in rep2.text and "Отзывов" in rep2.text,
       "ИИ недоступен — отчёт всё равно собирается")
    ok("Отзывов за этот месяц нет" in M.build_monthly(conn, M.month_period(2025, 1), NOW,
                                                       with_ai=False).text, "пустой месяц")

    print("\n=== расписание ===")
    at = time(10, 0)
    ok(M.monthly_due(True, at, "-1", None, NOW), "1-е число, 10:30 — пора")
    ok(not M.monthly_due(True, at, "-1", "2026-11", NOW), "уже отправлен в этом месяце")
    ok(not M.monthly_due(True, at, "-1", None, NOW.replace(hour=9)), "9:00 — рано")
    ok(not M.monthly_due(True, at, "-1", None, NOW + timedelta(days=1)), "2-го не догоняем")
    ok(not M.monthly_due(False, at, "-1", None, NOW), "выключен")

    print("\n=== в Telegram ===")
    asyncio.run(telegram(conn, p))
    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", text))

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append(("photo", caption))


class Msg:
    def __init__(self, photo=False):
        self.chat = SimpleNamespace(id=-100777)
        self.photo = [1] if photo else None
        self.out, self.edited, self.media = [], None, None

    async def answer(self, text, **kw):
        self.out.append(("text", text))
        return self

    async def answer_photo(self, photo, caption=None, **kw):
        self.out.append(("photo", caption))

    async def edit_text(self, text, **kw):
        self.edited = text

    async def edit_media(self, media, **kw):
        self.media = media

    async def delete(self):
        pass


async def telegram(conn, p):
    from telegram_ui import manage_ui

    m = Msg()
    await manage_ui.cmd_dynamics(m, conn)
    ok(m.out and m.out[0][0] == "photo", "/dynamics присылает график")
    cb = SimpleNamespace(data="dy:month:", message=Msg(photo=True), answer=lambda *a, **k: asyncio.sleep(0))
    await manage_ui.on_dynamics(cb, conn)
    ok(cb.message.media is not None, "переключение на месяцы — картинка меняется на месте")
    cb = SimpleNamespace(data="dy:week:abc", message=Msg(photo=True), answer=lambda *a, **k: asyncio.sleep(0))
    await manage_ui.on_dynamics(cb, conn)
    ok(cb.message.media is None, "кривые данные кнопки игнорируются")

    m = Msg()
    await manage_ui.cmd_branches(m, conn)
    ok(m.out and "Сравнение филиалов" in m.out[0][1], "/branches")

    fb = FakeBot()
    storage.set_state(conn, analytics.REPORT_CHAT, "-100777")
    os.environ.pop("GIGACHAT_CREDENTIALS", None)
    sent1 = await manage_ui.monthly_if_due(fb, conn, now=NOW)
    sent2 = await manage_ui.monthly_if_due(fb, conn, now=NOW + timedelta(minutes=1))
    ok(sent1 and not sent2, "отчёт 1-го числа ушёл один раз")
    ok([k for k, _ in fb.sent] == ["text", "text", "text", "photo"],
       "четыре сообщения: итоги, филиалы, темы, график")
    ok("Октябрь 2026".lower() in fb.sent[0][1], "за прошлый месяц")


if __name__ == "__main__":
    main()
