"""
Проверка внутренних комментариев (п. 5 ТЗ) и кнопки «Проблемный» (п. 6 ТЗ).

    DATABASE_URL=postgresql://... python test_comments.py
"""

import asyncio
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import analytics
import sheets
import storage
from parsers.base import Review, make_dedup_key
from telegram_ui import bot as B
from telegram_ui.cards import TZ, build_keyboard, render_card
from test_analytics import valid_html
from test_assignees import FakeBot, FakeCallback, setup

fails = 0


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


def add(conn, rating, text):
    r = Review(source="yandex", branch_name="Капучино",
               branch_address="Волгоград, улица Рокоссовского, 54",
               rating=rating, rating_confident=True, text=text,
               received_at=datetime.now(TZ) - timedelta(hours=1))
    r.dedup_key = make_dedup_key(r)
    return storage.save_review(conn, r).review_id


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


def buttons(card):
    return [label for row in build_keyboard(card) for label, _ in row]


class State:
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


class UserMsg:
    """Сообщение сотрудника с текстом комментария."""

    def __init__(self, bot, text, user_id=1, name="Анна"):
        self.bot, self.text = bot, text
        self.from_user = SimpleNamespace(id=user_id, full_name=name, username=None)
        self.chat = SimpleNamespace(id="-100ALL")
        self.replies = []

    async def answer(self, text, **kw):
        self.replies.append(text)


class PromptMsg(SimpleNamespace):
    async def reply(self, text, **kw):
        self.prompt = (text, kw.get("reply_markup"))

    answer = reply


async def scenario(conn):
    os.environ["NEGATIVE_CHAT_ID"] = "-100NEG"
    fb = FakeBot()

    print("=== кнопки на карточке ===")
    good = add(conn, 4, "Вкусно, но официант нагрубил и обсчитал")
    bad = add(conn, 2, "Холодный кофе")
    ok(buttons(B.card_for(conn, good))[-4:-2] == ["💬 Комментарий", "🚨 Проблемный"],
       "у 4★: «Комментарий» и «Проблемный»")
    ok("🚨 Проблемный" not in buttons(B.card_for(conn, bad)) and
       "💬 Комментарий" in buttons(B.card_for(conn, bad)),
       "у негатива по оценке «Проблемный» не нужен — он и так в негативе")

    print("\n=== комментарий ===")
    await B.deliver(fb, conn, "-100ALL", good, "-100NEG")
    copy = next(mid for (c, mid) in fb.messages if c == "-100ALL")
    cb = FakeCallback(fb, f"rv:comment:{good}", "-100ALL", copy, 1, "Анна")
    cb.message = PromptMsg(chat=SimpleNamespace(id="-100ALL"), message_id=copy)
    st = State()
    await B.on_button(cb, st, conn)
    text, markup = cb.message.prompt
    ok(st.state == B.CommentInput.waiting and type(markup).__name__ == "ForceReply",
       "бот ждёт текст, поле ответа открыто у нажавшей")
    ok("на площадку он не уходит" in text, "предупреждает, что комментарий внутренний")

    msg = UserMsg(fb, "Позвонили гостю, извинились, пригласили на кофе <за счёт заведения>")
    await B.receive_comment(msg, st, conn)
    ok("сохранён" in msg.replies[0] and st.state is None, "комментарий сохранён")
    card_text = plain(fb.messages[("-100ALL", copy)])
    ok("💬 Комментарии" in card_text and "Анна: Позвонили гостю" in card_text,
       "карточка в чате перерисовалась с комментарием")
    ok(valid_html(render_card(B.card_for(conn, good))), "угловые скобки в тексте не ломают разметку")

    for i, who in enumerate(["Борис", "Вера"]):
        storage.add_comment(conn, good, 10 + i, who, f"заметка {i + 1}")
    card = B.card_for(conn, good)
    t = plain(render_card(card))
    ok("💬 Комментарии (3)" in t and "Анна" not in t and "Вера: заметка 2" in t,
       "в карточке два последних, в заголовке — сколько всего")
    long = "очень длинный комментарий " * 20
    storage.add_comment(conn, good, 1, "Анна", long)
    ok("…" in render_card(B.card_for(conn, good)), "длинный обрезан в карточке")
    try:
        storage.add_comment(conn, good, 1, "Анна", "   ")
        ok(False, "пустой должен отказать")
    except ValueError:
        ok(True, "пустой комментарий не сохраняется")

    row = next(r for r in sheets.fetch_rows(conn) if "официант" in r[5])
    ok(sheets.HEADERS[-1] == "Комментарии" and len(row) == len(sheets.HEADERS),
       "в Google Таблице новая колонка «Комментарии»")
    ok(row[-1].count("\n") == 3 and row[-1].startswith("Анна: Позвонили"),
       "в таблице все комментарии, по строке на каждый")

    print("\n=== «Проблемный» ===")
    before = analytics.collect(conn, analytics.period_week()).negative
    cb = FakeCallback(fb, f"rv:problem:{good}", "-100ALL", copy, 2, "Борис")
    await B.on_button(cb, None, conn)
    card = B.card_for(conn, good)
    ok(card.is_negative and card.status == "attention", "стал «Требует внимания» с 🚨")
    ok(plain(render_card(card)).startswith("🚨") and "Отмечен проблемным: Борис" in plain(render_card(card)),
       "в карточке видно, кто отметил")
    chats = sorted({c for c, _ in storage.review_messages(conn, good)})
    ok(chats == ["-100ALL", "-100NEG"], f"копия ушла в чат «Негатив»: {chats}")
    ok(analytics.collect(conn, analytics.period_week()).negative == before + 1,
       "считается негативом в статистике")
    ok("↩️ Не проблемный" in buttons(card) and "🚨 Проблемный" not in buttons(card),
       "кнопка сменилась на «Не проблемный»")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reviews WHERE is_negative AND status = 'attention'")
        ok(good in [r["id"] for r in cur.fetchall()], "попадает в /negative")

    cb = FakeCallback(fb, f"rv:problem:{good}", "-100NEG", 999, 3, "Вера")
    await B.on_button(cb, None, conn)
    ok("уже" in cb.alerts[0][0], "повторное нажатие из другого чата — без дублей")

    cb = FakeCallback(fb, f"rv:unproblem:{good}", "-100ALL", copy, 2, "Борис")
    await B.on_button(cb, None, conn)
    card = B.card_for(conn, good)
    ok(not card.is_negative and card.status == "new" and not card.problem_by, "отметка снята, отзыв снова новый")
    cb = FakeCallback(fb, f"rv:unproblem:{bad}", "-100ALL", copy, 2, "Борис")
    await B.on_button(cb, None, conn)
    ok(B.card_for(conn, bad).is_negative, "негатив по оценке так не снимается")

    print("\n=== в разных статусах ===")
    storage.claim_review(conn, good, 1, "Анна")
    ok("🚨 Проблемный" in buttons(B.card_for(conn, good)), "в работе — можно отметить")
    storage.mark_problem(conn, good, "Анна")
    ok(B.card_for(conn, good).status == "in_progress", "взятый в работу остаётся в работе")
    storage.close_review(conn, good, "answered", 1, "Анна")
    labels = buttons(B.card_for(conn, good))
    ok("💬 Комментарий" in labels and "↩️ Не проблемный" not in labels,
       "закрытый: комментировать можно, отметку менять нельзя")
    ok(not storage.mark_problem(conn, good, "Вера"), "закрытый отметить нельзя")
    storage.mark_spam(conn, bad, 1, "Анна")
    ok("💬 Комментарий" not in buttons(B.card_for(conn, bad)), "у рекламы комментариев нет")


def main():
    conn = storage.connect(os.environ["DATABASE_URL"])
    setup(conn)
    asyncio.run(scenario(conn))
    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


if __name__ == "__main__":
    main()
