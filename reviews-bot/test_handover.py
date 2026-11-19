"""
Проверка того, что нужно к передаче проекта: права администратора
(ADMIN_IDS) и резервная копия базы с восстановлением.

    DATABASE_URL=postgresql://... python test_handover.py
"""

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg

import backup
import storage
from telegram_ui import access, backup_ui, menu_ui
from telegram_ui import bot as B
from test_analytics import TZ
from test_comments import scenario
from test_assignees import setup

fails = 0


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


def user(uid, bot=False):
    return SimpleNamespace(id=uid, full_name=f"user{uid}", username=None, is_bot=bot)


class Msg:
    def __init__(self, uid=1, text=""):
        self.from_user = user(uid)
        self.chat = SimpleNamespace(id=uid)
        self.text = text
        self.out = []
        self.bot = None

    async def answer(self, text, **kw):
        self.out.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.out.append(text)

    async def delete(self):
        pass


def cb(data, uid):
    c = SimpleNamespace(data=data, from_user=user(uid), message=Msg(uid), alerts=[])

    async def answer(text="", show_alert=False):
        c.alerts.append((text, show_alert))
    c.answer = answer
    return c


async def access_checks(conn):
    print("=== права администратора ===")
    os.environ.pop("ADMIN_IDS", None)
    ok(access.is_admin(user(5)) and not access.restricted(), "ADMIN_IDS не задан — ограничений нет, как раньше")

    os.environ["ADMIN_IDS"] = "111, 222; мусор"
    ok(access.admin_ids() == {111, 222}, "список читается, мусор пропускается")
    ok(access.is_admin(user(111)) and not access.is_admin(user(5)), "админ и сотрудник различаются")

    storage.set_negative_threshold(conn, 3)
    m = Msg(5)
    await B.cmd_threshold(m, conn, SimpleNamespace(args="1"))
    ok(storage.negative_threshold(conn) == 3 and access.DENIED in m.out[0], "сотрудник не меняет порог командой")
    m = Msg(5)
    await B.cmd_threshold(m, conn, SimpleNamespace(args=None))
    ok("Негативными считаются" in m.out[0], "но посмотреть текущий порог может")
    m = Msg(111)
    await B.cmd_threshold(m, conn, SimpleNamespace(args="2"))
    ok(storage.negative_threshold(conn) == 2, "администратор меняет")

    c = cb("mt:4", 5)
    await menu_ui.on_threshold(c, conn)
    ok(storage.negative_threshold(conn) == 2 and c.alerts[0] == (access.DENIED, True),
       "кнопка порога: сотруднику — всплывающее окно, порог не меняется")
    for action in ("threshold", "report_time", "report_here", "monthly_settings", "backup"):
        c = cb(f"ma:{action}", 5)
        await menu_ui.on_action(c, None, conn)
        ok(c.alerts and c.alerts[0][0] == access.DENIED and not c.message.out,
           f"меню «{action}» закрыто для сотрудника")
    for action in ("new", "stats", "guide", "demo"):
        c = cb(f"ma:{action}", 5)
        ok(action not in menu_ui.ADMIN_ACTIONS, f"«{action}» открыт для всех")

    from telegram_ui import stats_ui
    m = Msg(5)
    await stats_ui.cmd_report_here(m, conn)
    ok(access.DENIED in m.out[0], "/report_here — только администратор")
    m = Msg(5)
    await stats_ui.cmd_report_time(m, conn, SimpleNamespace(args="пт 18:00"))
    ok(access.DENIED in m.out[0], "/report_time с изменением — только администратор")

    m = Msg(5)
    await B.cmd_id(m)
    ok("Ваш Telegram ID: <code>5</code> — сотрудник" in m.out[0], "/id показывает свой ID и роль")
    os.environ.pop("ADMIN_IDS")


def backup_checks(conn, url):
    print("\n=== резервная копия ===")
    data, counts = backup.make_backup(conn)
    ok(data[:2] == b"PK" and counts["reviews"] >= 2 and counts["review_comments"] >= 4,
       f"архив {len(data) // 1024} КБ: {backup.describe(counts)}")
    meta, tables = backup.read_backup(data)
    ok(meta["format"] == backup.FORMAT_VERSION, "формат помечен")

    with conn.cursor() as cur:
        cur.execute("INSERT INTO emails (message_id, source, received_at, parsed_ok, raw) "
                    "VALUES ('<t@test>', 'yandex', now(), true, %s)", (b"\x00\x01binary\xff",))
    conn.commit()
    has_email = True
    data, counts = backup.make_backup(conn)

    # восстанавливаем в отдельную пустую базу
    admin = psycopg.connect(url, autocommit=True)
    admin.execute("DROP DATABASE IF EXISTS restore_check")
    admin.execute("CREATE DATABASE restore_check")
    admin.close()
    target_url = url.rsplit("/", 1)[0] + "/restore_check"
    target = storage.connect(target_url)
    restored = backup.restore(target, data)
    ok(restored == {t: counts.get(t, 0) for t in backup.TABLES}, f"восстановлено: {restored}")

    def snapshot(c):
        with c.cursor() as cur:
            cur.execute("SELECT id, status, is_negative, problem_by, warnings, received_at, "
                        "text FROM reviews ORDER BY id")
            reviews = cur.fetchall()
            cur.execute("SELECT review_id, user_name, text, created_at FROM review_comments ORDER BY id")
            comments = cur.fetchall()
            cur.execute("SELECT key, value FROM state ORDER BY key")
            state = cur.fetchall()
        c.commit()
        return reviews, comments, state

    ok(snapshot(conn) == snapshot(target), "отзывы, комментарии и настройки совпадают один в один")
    if has_email:
        with target.cursor() as cur:
            cur.execute("SELECT raw FROM emails ORDER BY id LIMIT 1")
            ok(bytes(cur.fetchone()["raw"]) == b"\x00\x01binary\xff", "оригиналы писем (байты) целы")
        target.commit()

    with target.cursor() as cur:
        cur.execute("INSERT INTO review_comments (review_id, text) VALUES "
                    "((SELECT min(id) FROM reviews), 'после восстановления') RETURNING id")
        new_id = cur.fetchone()["id"]
        cur.execute("SELECT max(id) AS m FROM review_comments WHERE id < %s", (new_id,))
        prev = cur.fetchone()["m"]
    target.commit()
    ok(new_id == prev + 1, "нумерация продолжается — новые записи не конфликтуют")

    restored_again = backup.restore(target, data)
    ok(restored_again == restored, "повторное восстановление поверх — без ошибок и дублей")
    target.close()

    lite, lite_counts = backup.make_backup(conn, with_raw_emails=False)
    ok(lite_counts == counts, "облегчённая копия без оригиналов писем — те же таблицы")


async def telegram_checks(conn):
    print("\n=== копия в Telegram ===")
    sent = []

    class FakeBot:
        async def send_document(self, chat_id, document, caption=None, **kw):
            sent.append((chat_id, document.filename, caption))

        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, None, text))

    os.environ["TELEGRAM_ADMIN_CHAT"] = "-100ADMIN"
    await backup_ui.send_backup(FakeBot(), "-100ADMIN", conn)
    ok(sent[0][1].startswith("reviews-backup-") and sent[0][1].endswith(".zip"), f"файл: {sent[0][1]}")
    ok("restore_backup.py" in sent[0][2], "в подписи — как восстановить")

    sunday = datetime(2026, 11, 1, 3, 5, tzinfo=TZ)
    ok(backup_ui.backup_due(None, sunday), "воскресенье 03:05 — пора")
    ok(not backup_ui.backup_due(None, sunday - timedelta(hours=1)), "02:05 — рано")
    ok(not backup_ui.backup_due(None, sunday + timedelta(days=1)), "понедельник — не догоняем")
    sent.clear()
    storage.set_state(conn, backup_ui.BACKUP_LAST, "")
    first = await backup_ui.backup_if_due(FakeBot(), conn, now=sunday)
    second = await backup_ui.backup_if_due(FakeBot(), conn, now=sunday + timedelta(minutes=1))
    ok(first and not second and len(sent) == 1, "раз в неделю, без повторов")


def main():
    url = os.environ["DATABASE_URL"]
    conn = storage.connect(url)
    setup(conn)
    asyncio.run(scenario(conn))  # отзывы, комментарии, «проблемный» — чтобы было что копировать
    asyncio.run(access_checks(conn))
    backup_checks(conn, url)
    asyncio.run(telegram_checks(conn))
    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


if __name__ == "__main__":
    main()
