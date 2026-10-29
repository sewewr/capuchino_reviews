"""
Проверка ИИ-аналитики по темам (п. 14 ТЗ) без обращения к GigaChat:
поддельная модель раскладывает отзывы по темам по ключевым словам.

    DATABASE_URL=postgresql://... python test_topics.py
"""

import asyncio
import json
import os
import re
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import analytics
import storage
import topics
from parsers.base import Review, make_dedup_key
from test_analytics import TZ, valid_html
from test_search import setup

fails = 0


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


KEYWORDS = {
    "торт": ("desserts", "+"), "медовик": ("desserts", "+"), "несвеж": ("desserts", "-"),
    "ждали": ("waiting", "-"), "холодн": ("temperature", "-"), "кассир": ("staff", "-"),
    "приветлив": ("staff", "+"), "уютно": ("interior", "+"), "не было": ("availability", "-"),
}


class FakeModel:
    """Отвечает как GigaChat: на разбор — JSON, на резюме — текст."""

    def __init__(self, broken=False, skip_last=False):
        self.calls = []
        self.broken, self.skip_last = broken, skip_last

    def chat(self, chat):
        system = chat.messages[0].content
        user = chat.messages[1].content
        self.calls.append(("summary" if "резюме" in system else "classify", user))
        if "резюме" in system:
            reply = "Гостям нравятся десерты, но на Рокоссовского жалуются на ожидание."
        elif self.broken:
            reply = "Извините, не могу помочь"
        else:
            answer = {}
            items = re.findall(r"^(\d+)\. \(.*?\) (.*)$", user, re.M)
            for n, text in items:
                found = {}
                for word, (topic, sign) in KEYWORDS.items():
                    if word in text.lower():
                        found[topic] = sign
                answer[n] = [[t, s] for t, s in found.items()]
            if self.skip_last and items:
                answer.pop(items[-1][0])
            reply = "Вот разбор:\n```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


NOW = datetime.now(TZ)


def at(days_ago, hour=12):
    return datetime.combine(NOW.date() - timedelta(days=days_ago), time(hour), TZ)


def add(conn, when, rating, text, address="Волгоград, улица Рокоссовского, 54"):
    r = Review(source="yandex", branch_name="Капучино", branch_address=address, rating=rating,
               rating_confident=True, text=text, received_at=when)
    r.dedup_key = make_dedup_key(r) + str(when)
    return storage.save_review(conn, r).review_id


def main():
    print("=== разбор ответа модели ===")
    raw = 'Конечно!\n```json\n{"1": [["desserts", "+"], ["waiting", "−"], ["мусор", "+"]], ' \
          '"2": [], "3.": [{"topic": "staff", "sign": "-"}], "9": [["drinks", "+"]]}\n```'
    parsed = topics.parse_answer(raw, 3)
    ok(parsed == {1: [("desserts", 1), ("waiting", -1)], 2: [], 3: [("staff", -1)]},
       f"JSON в обёртке, длинное тире, незнакомая тема, лишний номер: {parsed}")
    try:
        topics.parse_answer("не знаю", 2)
        ok(False, "без JSON должно отказать")
    except ValueError:
        ok(True, "ответ без JSON — понятная ошибка")
    ok(all(k in topics.classify_prompt() for k in topics.TOPICS), "все темы в промпте")

    conn = storage.connect(os.environ["DATABASE_URL"])
    setup(conn)
    # эта неделя
    add(conn, at(0), 5, "Медовик бесподобный, уютно")
    add(conn, at(1), 5, "Торт Алёнка — любовь, кассир приветливый")
    add(conn, at(2), 2, "Ждали 40 минут, кофе холодный")
    add(conn, at(3), 1, "Ждали заказ полчаса, кассир грубый")
    add(conn, at(4), 3, "Торт был несвежий, эклеров не было", address="Волгоград, переулок Ногина, 48")
    add(conn, at(5), 5, "")  # только звёзды
    add(conn, at(2), 5, "Помогаем бизнесу усиливать репутацию в Яндекс и 2ГИС. 2ГИС — 50 отзывов: 3 700 ₽")
    # прошлая неделя
    add(conn, at(9), 2, "Ждали долго", address="Волгоград, переулок Ногина, 48")

    print("\n=== разбор отзывов ===")
    model = FakeModel()
    done = topics.process_pending(conn, model)
    ok(done == 7, f"разобрано: {done} (реклама не разбирается)")
    ok(sum(1 for c in model.calls if c[0] == "classify") == 1, "все за один запрос к модели")
    ok("(без текста)" not in model.calls[0][1], "отзыв без текста в модель не ушёл")
    ok(topics.process_pending(conn, model) == 0 and len(model.calls) == 1,
       "повторно ничего не отправляется")

    print("\n=== что хвалят и ругают ===")
    week = analytics.period_week()
    st = topics.collect(conn, week)
    prev = topics.collect(conn, week.previous())
    ok(st.total == 5 and st.classified == 5, f"с текстом {st.total}, разобрано {st.classified}")
    ok([(t.topic, t.plus) for t in st.praised()] == [("desserts", 2), ("interior", 1), ("staff", 1)],
       f"хвалят: {[(t.topic, t.plus) for t in st.praised()]}")
    ok([(t.topic, t.minus) for t in st.criticized()][:2] == [("waiting", 2), ("availability", 1)],
       f"ругают: {[(t.topic, t.minus) for t in st.criticized()]}")
    ok(st.topics["staff"].plus == 1 and st.topics["staff"].minus == 1,
       "сотрудников и хвалят, и ругают — считается отдельно")
    ok(st.topics["waiting"].branches == [("ул. Рокоссовского, 54", 2)], "где ругают ожидание")

    text = topics.render(st, prev)
    ok(valid_html(text), "HTML корректный")
    print("\n" + "\n".join("    " + re.sub(r"<[^>]+>", "", l) for l in text.split("\n")) + "\n")
    ok("Время ожидания — 2 (▲ +1) · чаще всего ул. Рокоссовского, 54 (2)" in text,
       "динамика и проблемная зона")

    print("=== резюме от ИИ ===")
    summary = topics.summarize(conn, week, model)
    ok("Рокоссовского" in summary, f"резюме: {summary}")
    sent = [c[1] for c in model.calls if c[0] == "summary"][0]
    ok("Ждали заказ полчаса" in sent and "Ругают: Время ожидания 2" in sent,
       "модели переданы цифры и цитаты, жалобы первыми")
    ok(sent.index("1★") < sent.index("5★"), "сначала самые низкие оценки")
    ok("репутацию" not in sent, "реклама в резюме не попала")
    topics.summarize(conn, week, model)
    ok(sum(1 for c in model.calls if c[0] == "summary") == 1, "то же резюме — из кэша, без запроса")

    block = topics.report_block(st, summary)
    ok(valid_html(block) and "👎 Ругают: время ожидания (2)" in block, "блок для еженедельного отчёта")

    print("\n=== полный текст — темы заново ===")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reviews WHERE text LIKE 'Медовик%'")
        rid = cur.fetchone()["id"]
    storage.add_full_text(conn, rid, "Медовик бесподобный, уютно, но ждали 20 минут")
    topics.process_pending(conn, model)
    with conn.cursor() as cur:
        cur.execute("SELECT topic, sentiment FROM review_topics WHERE review_id = %s ORDER BY topic", (rid,))
        got = [(r["topic"], r["sentiment"]) for r in cur.fetchall()]
    conn.commit()
    ok(("waiting", -1) in got, f"после добавления полного текста: {got}")

    print("\n=== модель ответила ерундой ===")
    add(conn, at(0, 9), 4, "Торт вкусный")
    import drafts
    try:
        topics.process_pending(conn, FakeModel(broken=True))
        ok(False, "должна подняться понятная ошибка")
    except drafts.DraftError as e:
        ok("непонятный ответ" in str(e), f"понятная ошибка: {e}")
    ok(topics.pending(conn, 10), "отзыв остался в очереди — разберём позже")
    topics.process_pending(conn, FakeModel(skip_last=True))
    ok(not topics.pending(conn, 10), "пропущенный моделью отзыв не уходит в модель снова и снова")

    print("\n=== /topics в Telegram ===")
    from telegram_ui import stats_ui

    os.environ.pop("GIGACHAT_CREDENTIALS", None)
    problem = asyncio.run(stats_ui.catch_up_topics(conn))
    ok("не подключён" in problem, f"без ключа GigaChat: {problem}")
    text = stats_ui.topics_text(conn, "month", problem)
    ok(valid_html(text) and "месяц" in text, "месячный разрез открывается")
    kb = stats_ui.topics_keyboard("month")
    ok(kb.inline_keyboard[1][0].callback_data == "tp:sum:month", "резюме за выбранный период")
    ok(asyncio.run(stats_ui.weekly_topics(conn, NOW)) == "", "без ИИ отчёт уходит без блока тем")

    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


if __name__ == "__main__":
    main()
