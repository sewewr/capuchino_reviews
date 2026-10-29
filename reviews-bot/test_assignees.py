"""
Проверка ответственных (п. 8 ТЗ) без Telegram: подставляем поддельного бота
и поддельные нажатия кнопок.

    DATABASE_URL=postgresql://... python test_assignees.py
"""

import asyncio
import os
import re
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import storage
from parsers.base import Review, make_dedup_key
from telegram_ui import bot as B
from telegram_ui.cards import TZ


class FakeBot:
    """Бот-заглушка: хранит, что лежит в каждом сообщении каждого чата."""

    def __init__(self):
        self.messages = {}  # (chat_id, message_id) -> текст
        self.next_id = 100

    async def send_message(self, chat_id, text, **kw):
        self.next_id += 1
        self.messages[(str(chat_id), self.next_id)] = text
        return SimpleNamespace(message_id=self.next_id, chat=SimpleNamespace(id=chat_id))

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.messages[(str(chat_id), message_id)] = text

    def heads(self):
        """Первая строка и строка с ответственным каждой копии — для печати."""
        out = []
        for (chat, mid), text in sorted(self.messages.items()):
            clean = re.sub(r"</?(b|i|code)>", "", text)
            head = clean.split("\n")[0]
            who = next((l for l in clean.split("\n") if l.startswith("🙋")), "")
            out.append(f"{chat:<8} {head:<38} {who}")
        return out


class FakeCallback:
    """Нажатие кнопки на конкретной копии карточки."""

    def __init__(self, bot, data, chat_id, message_id, user_id, name):
        self.bot = bot
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
        self.from_user = SimpleNamespace(id=user_id, full_name=name, username=None)
        self.alerts = []

    async def answer(self, text="", show_alert=False):
        self.alerts.append((text, show_alert))


def setup(conn):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS review_topics, drafts, review_messages, reviews, emails, branches, state CASCADE")
    conn.commit()
    storage.init_schema(conn)
    from seed_branches import BRANCHES, SOURCES
    for source in SOURCES:
        for b in BRANCHES:
            storage.upsert_branch(conn, source, b["name"], b["address"])
    conn.commit()


def add(conn, rating, text):
    r = Review(source="yandex", branch_name="Капучино",
               branch_address="Волгоград, улица Рокоссовского, 54",
               rating=rating, rating_confident=True, text=text,
               received_at=datetime.now(TZ) - timedelta(hours=1))
    r.dedup_key = make_dedup_key(r)
    return storage.save_review(conn, r).review_id


def copy_of(fb, chat):
    return next(mid for (c, mid) in fb.messages if c == chat)


async def scenario(conn):
    fb = FakeBot()
    rid = add(conn, 2, "Холодный кофе, ждали 40 минут")

    print("--- негатив пришёл в два чата ---")
    await B.deliver(fb, conn, "-100ALL", rid, "-100NEG")
    for line in fb.heads():
        print("  " + line)
    print(f"  копий запомнено: {len(storage.review_messages(conn, rid))}")

    print("\n--- Анна берёт отзыв в чате «Негатив» ---")
    cb = FakeCallback(fb, f"rv:take:{rid}", "-100NEG", copy_of(fb, "-100NEG"), 1, "Анна")
    await B.on_button(cb, None, conn)
    print(f"  Анне ответили: {cb.alerts[0][0]}")
    for line in fb.heads():
        print("  " + line)

    print("\n--- Борис жмёт «Взять» на старой копии в общем чате ---")
    cb2 = FakeCallback(fb, f"rv:take:{rid}", "-100ALL", copy_of(fb, "-100ALL"), 2, "Борис")
    await B.on_button(cb2, None, conn)
    print(f"  Борису ответили: {cb2.alerts[0][0]}  (всплывающее окно: {cb2.alerts[0][1]})")
    with conn.cursor() as cur:
        cur.execute("SELECT assignee_name FROM reviews WHERE id = %s", (rid,))
        print(f"  в базе ответственный: {cur.fetchone()['assignee_name']}  (ждём Анна)")

    print("\n--- Анна возвращает отзыв в очередь ---")
    cb3 = FakeCallback(fb, f"rv:release:{rid}", "-100NEG", copy_of(fb, "-100NEG"), 1, "Анна")
    await B.on_button(cb3, None, conn)
    for line in fb.heads():
        print("  " + line)

    print("\n--- Борис закрывает отзыв, не беря в работу ---")
    cb4 = FakeCallback(fb, f"rv:done:{rid}", "-100ALL", copy_of(fb, "-100ALL"), 2, "Борис")
    await B.on_button(cb4, None, conn)
    for line in fb.heads():
        print("  " + line)

    print("\n--- карточка, отправленная до обновления (копия не запомнена) ---")
    rid2 = add(conn, 5, "Отличные десерты")
    old = await fb.send_message("-100ALL", "старая карточка без учёта копий")
    cb5 = FakeCallback(fb, f"rv:take:{rid2}", "-100ALL", old.message_id, 3, "Вера")
    await B.on_button(cb5, None, conn)
    text = re.sub(r"</?(b|i|code)>", "", fb.messages[("-100ALL", old.message_id)])
    print(f"  обновилась: {text.split(chr(10))[0]}")


def race(url):
    """Двое жмут «Взять в работу» одновременно — с разных подключений."""
    conn = storage.connect(url)
    rid = add(conn, 1, "Гонка")
    conn.close()

    results = {}
    barrier = threading.Barrier(2)

    def press(user_id, name):
        c = storage.connect(url)
        barrier.wait()
        results[name] = storage.claim_review(c, rid, user_id, name)
        c.close()

    threads = [threading.Thread(target=press, args=(10, "Глеб")),
               threading.Thread(target=press, args=(11, "Дина"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [n for n, (won, _) in results.items() if won]
    print("\n--- одновременное нажатие ---")
    for name, (won, holder) in sorted(results.items()):
        print(f"  {name}: {'взял(а)' if won else f'отказ, у {holder}'}")
    print(f"  победителей: {len(winners)}  (ждём ровно 1)")


def main():
    url = os.environ["DATABASE_URL"]
    conn = storage.connect(url)
    setup(conn)
    asyncio.run(scenario(conn))
    conn.close()
    race(url)


if __name__ == "__main__":
    main()
