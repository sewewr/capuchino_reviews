"""
Проверка меню с кнопками без Telegram.

    DATABASE_URL=postgresql://... python test_menu.py
"""

import asyncio
import os
import re
from types import SimpleNamespace

import analytics
import storage
from telegram_ui import bot as B
from telegram_ui import menu_ui, search_ui, stats_ui
from test_analytics import valid_html
from test_search import setup

fails = 0


def ok(cond, label):
    global fails
    fails += not cond
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")


class Msg:
    def __init__(self):
        self.chat = SimpleNamespace(id=-1001234)
        self.out, self.edited, self.markup = [], None, None
        self.bot = None

    async def answer(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))
        return self

    async def edit_text(self, text, **kw):
        self.edited, self.markup = text, kw.get("reply_markup")


class State:
    def __init__(self):
        self.state, self.data = None, {}

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)


def callback(data):
    cb = SimpleNamespace(data=data, message=Msg(), answers=[],
                         from_user=SimpleNamespace(id=7, full_name="Анна", username=None))

    async def answer(text="", **kw):
        cb.answers.append(text)
    cb.answer = answer
    return cb


def all_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def main():
    print("=== разделы ===")
    root = menu_ui.root_keyboard()
    labels = [b.text for b in all_buttons(root)]
    ok(labels == ["📥 Отзывы", "📊 Аналитика", "⚙️ Настройки", "❓ Помощь"], f"разделы: {labels}")
    ok([len(r) for r in root.inline_keyboard] == [2, 2], "по две кнопки в ряд")
    for key in menu_ui.SECTIONS:
        kb = menu_ui.section_keyboard(key)
        ok(all_buttons(kb)[-1].callback_data == "mn:root", f"{menu_ui.SECTIONS[key][0]}: есть «← Назад»")
    every = [b for k in menu_ui.SECTIONS for b in all_buttons(menu_ui.section_keyboard(k))]
    every += all_buttons(menu_ui.threshold_keyboard(3)) + all_buttons(menu_ui.schedule_keyboard(True))
    ok(all(len(b.callback_data.encode()) <= 64 for b in every), "все кнопки в лимите Telegram 64 байта")

    cb = callback("mn:analytics")
    asyncio.run(menu_ui.on_section(cb))
    ok("📊 Аналитика" in cb.message.edited and
       [b.text for b in all_buttons(cb.message.markup)][0] == "📊 Статистика",
       "нажали раздел — сообщение перерисовалось на месте")
    cb = callback("mn:root")
    asyncio.run(menu_ui.on_section(cb))
    ok(cb.message.edited == menu_ui.ROOT_TEXT, "«Назад» — к разделам")

    print("\n=== каждая кнопка что-то делает ===")
    calls = []

    def fake(name):
        async def f(*a, **kw):
            calls.append(name)
        return f

    patches = {
        (B, "cmd_new"): "new", (B, "cmd_negative"): "negative", (B, "cmd_export"): "export",
        (B, "cmd_id"): "id", (B, "cmd_demo"): "demo", (B, "cmd_clear_demo"): "clear_demo",
        (search_ui, "cmd_reviews"): "reviews", (search_ui, "cmd_spam"): "spam",
        (stats_ui, "cmd_stats"): "stats", (stats_ui, "cmd_topics"): "topics",
        (stats_ui, "cmd_report"): "report", (stats_ui, "cmd_report_here"): "report_here",
    }
    originals = {k: getattr(*k) for k in patches}
    for (mod, name), action in patches.items():
        setattr(mod, name, fake(action))

    conn = storage.connect(os.environ["DATABASE_URL"])
    setup(conn)
    try:
        for key, (_, _, items) in menu_ui.SECTIONS.items():
            for label, action in items:
                cb, st = callback(f"ma:{action}"), State()
                before = len(calls)
                asyncio.run(menu_ui.on_action(cb, st, conn))
                did = (len(calls) > before or cb.message.out or cb.message.edited)
                ok(bool(did), f"{label}")
    finally:
        for (mod, name), fn in originals.items():
            setattr(mod, name, fn)

    print("\n=== поиск из меню ===")
    cb, st = callback("ma:find"), State()
    asyncio.run(menu_ui.on_action(cb, st, conn))
    text, markup = cb.message.out[0]
    ok(st.state == search_ui.SearchInput.waiting, "бот ждёт слово для поиска")
    ok(type(markup).__name__ == "ForceReply" and markup.selective,
       "поле ответа открывается только у нажавшего — текст дойдёт и в группе")
    ok('tg://user?id=7' in text, "сотрудник упомянут")

    print("\n=== порог негатива кнопками ===")
    cb = callback("ma:threshold")
    asyncio.run(menu_ui.on_action(cb, State(), conn))
    ok("✓ 1–3★" in [b.text for b in all_buttons(cb.message.markup)], "текущий порог отмечен")
    cb = callback("mt:2")
    asyncio.run(menu_ui.on_threshold(cb, conn))
    ok(storage.negative_threshold(conn) == 2, "порог сменился на 2")
    ok("✓ 1–2★" in [b.text for b in all_buttons(cb.message.markup)], "отметка переехала")

    print("\n=== расписание отчёта кнопками ===")
    cb = callback("mr:пт 18:00")
    asyncio.run(menu_ui.on_schedule(cb, conn))
    s = analytics.load_schedule(conn, "-1")
    ok((s.day, s.at.strftime("%H:%M")) == (4, "18:00"), f"стало: {s.describe()}")
    cb = callback("mr:off")
    asyncio.run(menu_ui.on_schedule(cb, conn))
    ok(not analytics.load_schedule(conn, "-1").enabled, "отчёт выключен")
    ok("🔔 Включить отчёт" in [b.text for b in all_buttons(cb.message.markup)], "кнопка сменилась на «Включить»")
    asyncio.run(menu_ui.on_schedule(callback("mr:on"), conn))
    ok(analytics.load_schedule(conn, "-1").enabled, "включён обратно")

    print("\n=== тексты ===")
    for text in (menu_ui.GUIDE, menu_ui.commands_text(), menu_ui.ROOT_TEXT,
                 *[menu_ui.section_text(k) for k in menu_ui.SECTIONS]):
        ok(valid_html(text), f"HTML корректный: «{re.sub('<[^>]+>', '', text)[:35]}…»")
    print("\n" + "\n".join("    " + re.sub("<[^>]+>", "", l) for l in menu_ui.commands_text().split("\n")))

    print("\n=== команды Telegram ===")
    B._include_routers()
    registered = set()
    for router in [B.dp, *B.dp.sub_routers]:
        for h in router.message.handlers:
            for f in h.filters or []:
                cmds = getattr(f.callback, "commands", None)
                if cmds:
                    registered |= {c if isinstance(c, str) else c.command for c in cmds}
    missing = [c for c, _ in menu_ui.COMMANDS if c not in registered]
    ok(not missing, f"у каждой команды из меню Telegram есть обработчик {missing or ''}")
    ok(all(3 <= len(d) <= 256 and re.fullmatch(r"[a-z0-9_]{1,32}", c) for c, d in menu_ui.COMMANDS),
       "названия и описания в рамках правил Telegram")

    sent = []

    class FakeBot:
        async def set_my_commands(self, commands, scope=None):
            sent.append((type(scope).__name__, len(commands)))
    asyncio.run(menu_ui.set_commands(FakeBot()))
    ok([s[0] for s in sent] == ["BotCommandScopeAllPrivateChats", "BotCommandScopeAllGroupChats"],
       "список команд — и в личке, и в группах")

    class Broken:
        async def set_my_commands(self, *a, **kw):
            raise RuntimeError("Telegram недоступен")
    asyncio.run(menu_ui.set_commands(Broken()))
    ok(True, "сбой при регистрации команд не роняет запуск")

    m = Msg()
    asyncio.run(B.cmd_start(m))
    ok(m.out[0][1] is not None and len(m.out[0][0]) < 300, "/start — коротко и сразу меню")
    conn.close()
    print(f"\n{'Всё прошло' if not fails else f'ОШИБОК: {fails}'}")


if __name__ == "__main__":
    main()
