"""
Проверка конвейера целиком: почта → парсер → база → отправка карточек.

Настоящий IMAP-сервер здесь не нужен: подставляем заглушку, которая отдаёт
реальные письма из samples/ так же, как это делал бы ящик.

    DATABASE_URL=postgresql://... python test_pipeline.py
"""

import os
import re
from datetime import date, timedelta
from pathlib import Path

import pipeline
import storage
from mailbox import Letter, MailboxError
from telegram_ui.cards import CardData, render_card


class FakeMailbox:
    """Ящик из файлов: ведёт себя как Mailbox, но ничего не качает."""

    def __init__(self, paths, uid_validity="1000", broken_uid=None):
        self.letters = {}
        for i, path in enumerate(paths, start=1):
            raw = Path(path).read_bytes()
            import email
            from email import policy

            msg = email.message_from_bytes(raw, policy=policy.default)
            self.letters[i] = Letter(
                uid=i,
                message_id=str(msg.get("Message-ID", "")) or None,
                sender=str(msg.get("From", "")),
                subject=str(msg.get("Subject", "")),
                raw=raw,
            )
        self._uid_validity = uid_validity
        self.broken_uid = broken_uid
        self.fetched = []

    def uid_validity(self):
        return self._uid_validity

    def search(self, senders, since, min_uid=0):
        return [
            uid for uid, letter in self.letters.items()
            if uid > min_uid and any(s in letter.sender for s in senders)
        ]

    def message_ids(self, uids):
        return {uid: self.letters[uid].message_id for uid in uids}

    def fetch(self, uid):
        if uid == self.broken_uid:
            raise MailboxError("связь оборвалась")
        self.fetched.append(uid)
        return self.letters[uid]


def setup():
    conn = storage.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS review_comments, review_topics, drafts, review_messages, reviews, emails, branches, state CASCADE")
    conn.commit()
    storage.init_schema(conn)

    from seed_branches import BRANCHES, SOURCES

    for source in SOURCES:
        for b in BRANCHES:
            storage.upsert_branch(conn, source, b["name"], b["address"])
    conn.commit()
    return conn


def collect(conn):
    """Куда ушли бы карточки — вместо Telegram просто копим id."""
    sent = []
    return sent, lambda review_id, branch_id: sent.append(review_id)


def main():
    conn = setup()
    samples = sorted(str(p) for p in Path("samples").glob("*.eml"))
    since = date(2026, 1, 1)

    print("--- первый проход ---")
    box = FakeMailbox(samples)
    sent, on_review = collect(conn)
    r = pipeline.poll_once(conn, box, since=since, on_review=on_review)
    print(f"  {r.summary()}")
    print(f"  карточек отправлено: {len(sent)}")
    print(f"  писем скачано: {len(box.fetched)}")

    print("\n--- повторный проход (письма те же) ---")
    box2 = FakeMailbox(samples)
    sent2, on_review2 = collect(conn)
    r2 = pipeline.poll_once(conn, box2, since=since, on_review=on_review2)
    print(f"  {r2.summary()}")
    print(f"  писем скачано повторно: {len(box2.fetched)}  (ждём 0)")

    print("\n--- ящик пересоздан: UIDVALIDITY сменился ---")
    box3 = FakeMailbox(samples, uid_validity="2000")
    sent3, on_review3 = collect(conn)
    r3 = pipeline.poll_once(conn, box3, since=since, on_review=on_review3)
    print(f"  {r3.summary()}")
    print(f"  новых карточек: {len(sent3)}  (ждём 0 — спасает Message-ID)")

    print("\n--- обрыв связи на втором письме ---")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reviews")
        cur.execute("DELETE FROM emails")
        cur.execute("DELETE FROM state")
    conn.commit()
    box4 = FakeMailbox(samples, broken_uid=2)
    sent4, on_review4 = collect(conn)
    r4 = pipeline.poll_once(conn, box4, since=since, on_review=on_review4)
    print(f"  {r4.summary()}")
    print(f"  обработано до обрыва: {len(sent4)}")
    print(f"  отметка UID: {storage.get_state(conn, pipeline.STATE_LAST_UID)}  (ждём 1)")

    print("\n--- битое письмо не роняет остальные ---")
    broken = Path("/tmp/broken.eml")
    broken.write_bytes(
        (
            "From: Yandex <no-reply@business.yandex.ru>\r\n"
            "Subject: broken\r\n"
            "Message-ID: <broken@test>\r\n\r\n"
            "ни HTML, ни отзывов\r\n"
        ).encode()
    )
    box5 = FakeMailbox([str(broken)] + samples)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reviews")
        cur.execute("DELETE FROM emails")
        cur.execute("DELETE FROM state")
    conn.commit()
    sent5, on_review5 = collect(conn)
    r5 = pipeline.poll_once(conn, box5, since=since, on_review=on_review5)
    print(f"  {r5.summary()}")
    print(f"  карточек всё равно отправлено: {len(sent5)}")
    with conn.cursor() as cur:
        cur.execute("SELECT message_id, parsed_ok, error FROM emails WHERE NOT parsed_ok")
        for row in cur.fetchall():
            print(f"  сохранено для разбора позже: {row['message_id']} — {row['error']}")

    print("\n--- контроль тишины ---")
    # письма в samples/ августовские и сентябрьские, поэтому молчание настоящее
    print(f"  по письмам из samples: {pipeline.silent_sources(conn)}")
    just_after = max(
        storage.last_letter_at(conn, s) for s in ("yandex", "2gis")
    ) + timedelta(days=1)
    print(f"  если бы письма были свежими: {pipeline.silent_sources(conn, now=just_after)}  (ждём пусто)")

    print("\n--- предупреждение о тишине не повторяется после перезапуска ---")
    import asyncio
    from types import SimpleNamespace

    from telegram_ui import bot as B

    sent_alerts = []

    class AlertBot:
        async def send_message(self, chat_id, text, **kw):
            sent_alerts.append(text)

    cfg = SimpleNamespace(telegram_admin_chat="-100ADMIN")
    for _restart in range(3):  # каждый «перезапуск» — без памяти о прошлом
        asyncio.run(B.check_silence(AlertBot(), conn, cfg))
    silent_now = len(pipeline.silent_sources(conn))
    print(f"  предупреждений после 3 перезапусков: {len(sent_alerts)}  (ждём {silent_now} — по одному на площадку)")
    with conn.cursor() as cur:
        cur.execute("UPDATE emails SET received_at = received_at + interval '1 day' "
                    "WHERE id = (SELECT max(id) FROM emails WHERE source = 'yandex')")
    conn.commit()
    asyncio.run(B.check_silence(AlertBot(), conn, cfg))
    print(f"  пришло новое письмо, площадка снова молчит: {len(sent_alerts)}  (ждём {silent_now + 1})")

    print("\n--- что ушло бы в Telegram ---")
    for review_id in sent5[:2]:
        card = CardData(**dict(storage.get_card_data(conn, review_id)))
        print()
        print(re.sub(r"</?(b|i|code)>", "", render_card(card)))

    conn.close()


if __name__ == "__main__":
    main()
