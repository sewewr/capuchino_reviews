"""
Проверка ИИ-черновиков без обращения к GigaChat: подставляем поддельную
модель, которая отвечает заготовками и запоминает, что ей прислали.

    DATABASE_URL=postgresql://... python test_drafts.py
"""

import asyncio
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import drafts
import storage
from parsers.base import Review, make_dedup_key
from telegram_ui.cards import TZ


class FakeGigaChat:
    """Модель-заглушка. answers — что отвечать по очереди."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, chat):
        msgs = {m.role.value if hasattr(m.role, "value") else m.role: m.content
                for m in chat.messages}
        self.calls.append({"system": msgs["system"], "user": msgs["user"],
                           "temperature": chat.temperature})
        text = self.answers.pop(0) if self.answers else "Спасибо!"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def review(**kw):
    base = dict(review_id=1, source="yandex", rating=5, text="Очень вкусный медовик",
                text_truncated=False, author=None,
                branch_address="ул. Рокоссовского, 54, Волгоград", is_negative=False)
    base.update(kw)
    return drafts.ReviewForDraft(**base)


def ok(cond, label):
    print(f"  {'✓' if cond else '✗ ОШИБКА'} {label}")
    return cond


def unit_tests():
    print("=== промпт ===")
    brand = drafts.Brand()
    fake = FakeGigaChat(["Медовик — наша гордость, рады, что понравился. Ждём Вас снова!"])
    d = drafts.generate(review(author="Мария"), client=fake, brand=brand)
    call = fake.calls[0]
    ok("Капучино" in call["system"], "бренд в системном промпте")
    ok("отметь конкретную деталь" in call["system"], "правило для позитива из п. 9 ТЗ")
    ok("не спорь" in call["system"], "правило для негатива из п. 9 ТЗ")
    ok("Не обещай компенсаций" in call["system"], "по умолчанию — без компенсаций")
    ok("Не указывай телефоны" in call["system"], "без контакта — не выдумывать телефон")
    ok("Имя гостя: Мария" in call["user"], "имя гостя передаётся")
    ok("Оценка: 5 из 5" in call["user"], "оценка передаётся")
    ok("медовик" in call["user"].lower(), "текст отзыва передаётся")
    ok(d.text.startswith("Медовик"), "ответ вернулся")

    print("\n=== настройки бренда ===")
    fake = FakeGigaChat(["Ответ"])
    drafts.generate(review(rating=2, is_negative=True), client=fake,
                    brand=drafts.Brand(contact="+7 (8442) 00-00-00", allow_compensation=True))
    sys_ = fake.calls[0]["system"]
    ok("+7 (8442) 00-00-00" in sys_, "контакт для негатива подставлен")
    ok("загладить вину" in sys_, "компенсация разрешена, когда включена")

    print("\n=== обрезанный отзыв ===")
    fake = FakeGigaChat(["Спасибо за тёплые слова о десертах."])
    d = drafts.generate(review(text="Мой любимый торт — Роскошная Блондинка, кто не пробовал, оч...",
                               text_truncated=True), client=fake, brand=brand)
    ok("обрезан площадкой" in fake.calls[0]["user"], "модель предупреждена не додумывать")
    ok(any("обрезан" in w for w in d.warnings), "сотрудник предупреждён в черновике")

    print("\n=== чистка ответа ===")
    ok(drafts.clean('Ответ: «Спасибо!»') == "Спасибо!", "убраны «Ответ:» и кавычки")
    text, warns = drafts.check("Спасибо!!! Пишите в @capuchino_vlg или t.me/capuchino ОЧЕНЬ ждём", brand)
    ok("!!!" not in text, "лишние восклицательные знаки схлопнуты")
    ok("@capuchino" not in text and "t.me" not in text, "ссылки на Telegram убраны")
    ok(any("капсом" in w for w in warns), "предупреждение о капсе")
    long_text, warns = drafts.check("Очень длинный ответ. " * 200, brand)
    ok(len(long_text) <= drafts.MAX_LENGTH, f"обрезка до {drafts.MAX_LENGTH} знаков ({len(long_text)})")
    ok(long_text.endswith("."), "обрезано по границе предложения")

    print("\n=== ответы не начинаются одинаково ===")
    recent = ["Спасибо за отзыв, Мария"]
    fake = FakeGigaChat(["Спасибо за отзыв, рады видеть.", "Как приятно читать такое!"])
    d = drafts.generate(review(), recent_openings=recent, client=fake, brand=brand)
    ok(len(fake.calls) == 2, "повтор начала — запрошен ещё один вариант")
    ok(d.text.startswith("Как приятно"), "взят новый вариант")
    ok("НЕ начинай похоже" in fake.calls[0]["system"], "модели показаны недавние начала")

    print("\n=== варианты из ТЗ ===")
    for style in ("another", "shorter", "warmer", "formal"):
        fake = FakeGigaChat([f"вариант {style}"])
        d = drafts.generate(review(), style=style, previous="Прошлый черновик",
                            client=fake, brand=brand)
        ok("Прошлый черновик" in fake.calls[0]["user"] and d.style == style,
           f"«{style}»: предыдущий вариант передан")
    ok(drafts.STYLE_TEMPERATURE["another"] > drafts.STYLE_TEMPERATURE["base"],
       "«другой вариант» пишется смелее")
    try:
        drafts.generate(review(), style="shorter", client=FakeGigaChat([]), brand=brand)
        ok(False, "«короче» без исходного черновика должно отказать")
    except drafts.DraftError:
        ok(True, "«короче» без исходного черновика — понятная ошибка")

    print("\n=== GigaChat недоступен ===")

    class Broken:
        def chat(self, chat):
            raise ConnectionError("timeout")
    try:
        drafts.generate(review(), client=Broken(), brand=brand)
        ok(False, "ошибка должна подняться")
    except drafts.DraftError as e:
        ok("подключиться к GigaChat" in str(e), f"понятная ошибка вместо падения: {e}")

    print("\n=== модель переименовали: «No such model» ===")
    from gigachat.exceptions import NotFoundError, ResponseError
    import httpx

    url = "https://api.giga.chat/v1/chat/completions"
    headers = httpx.Headers({"x-request-id": "abc", "server": "nginx"})
    no_model = b'{"status":404,"message":"No such model"}'

    class Renamed(FakeGigaChat):
        """Знает только GigaChat-2-Pro; старое имя отвергает, как настоящий API."""

        def __init__(self, models):
            super().__init__(["Спасибо, что заглянули!"])
            self.models = models
            self.asked_models = []

        def chat(self, chat):
            self.asked_models.append(chat.model)
            if chat.model not in self.models:
                raise NotFoundError(url, 404, no_model, headers)
            return super().chat(chat)

        def get_models(self):
            return SimpleNamespace(data=[SimpleNamespace(id_=m) for m in
                                         ["Embeddings", *self.models]])

    os.environ["GIGACHAT_MODEL"] = "GigaChat"  # как было в Railway
    drafts._model = None
    fake = Renamed(["GigaChat-2-Pro", "GigaChat-2-Max"])
    d = drafts.generate(review(), client=fake, brand=brand)
    ok(d.text.startswith("Спасибо"), "черновик всё равно получился")
    ok(fake.asked_models == ["GigaChat", "GigaChat-2-Pro"],
       f"после 404 бот сам выбрал доступную модель: {fake.asked_models}")
    drafts.generate(review(), client=fake, brand=brand)
    ok(fake.asked_models[-1] == "GigaChat-2-Pro" and len(fake.asked_models) == 3,
       "выбор запомнен — повторной ошибки нет")

    drafts._model = None
    os.environ.pop("GIGACHAT_MODEL")
    ok(drafts.current_model() == "GigaChat-2", "по умолчанию — GigaChat-2")
    ok(drafts.pick_model(["Embeddings", "GigaChat-2-Max", "GigaChat-2"]) == "GigaChat-2",
       "из списка выбирается самая лёгкая модель")
    ok(drafts.pick_model(["Embeddings", "GigaChat-3-Ultra"]) == "GigaChat-3-Ultra",
       "незнакомая новая линейка тоже подойдёт")
    ok(drafts.pick_model(["Embeddings"]) is None, "без чат-моделей — не выбираем эмбеддинги")

    drafts._model = None
    fake = Renamed([])  # у аккаунта нет ни одной чат-модели
    try:
        drafts.generate(review(), client=fake, brand=brand)
        ok(False, "ошибка должна подняться")
    except drafts.DraftError as e:
        msg = str(e)
        ok("GIGACHAT_MODEL" in msg, f"понятная причина: {msg}")
        ok("Headers" not in msg and "x-request-id" not in msg and len(msg) < 150,
           "без простыни HTTP-заголовков")

    print("\n=== короткие причины по кодам ответа ===")
    for code, expect in [(401, "ключ"), (402, "токены"), (429, "подождите"), (503, "временно")]:
        msg = drafts.explain(ResponseError(url, code, b"{}", headers))
        ok(expect in msg and "Headers" not in msg, f"{code}: {msg}")
    drafts._model = None


# --- сквозная проверка в боте -------------------------------------------------


class FakeMsg:
    def __init__(self, bot, chat_id=-100, text=""):
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = bot.next_id()
        self.text = text
        self.markup = None
        bot.log.append(self)

    async def reply(self, text, **kw):
        return FakeMsg(self.bot, self.chat.id, text)

    async def answer(self, text, **kw):
        return FakeMsg(self.bot, self.chat.id, text)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text, self.markup = text, reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup


class FakeBot:
    def __init__(self):
        self._id, self.log, self.edits = 1000, [], []

    def next_id(self):
        self._id += 1
        return self._id

    async def send_message(self, chat_id, text, **kw):
        return FakeMsg(self, chat_id, text)

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.edits.append((chat_id, message_id, text))


class FakeCallback:
    def __init__(self, bot, data, message, user_id=1, name="Анна"):
        self.bot, self.data, self.message = bot, data, message
        self.from_user = SimpleNamespace(id=user_id, full_name=name, username=None)
        self.alerts = []

    async def answer(self, text="", show_alert=False):
        self.alerts.append(text)


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


def plain(text):
    return re.sub(r"</?(b|i|code|pre)>", "", text)


async def flow(conn):
    from telegram_ui import bot as B
    from telegram_ui import draft_ui

    os.environ["GIGACHAT_CREDENTIALS"] = "fake"
    fake = FakeGigaChat([
        "Мария, спасибо! Медовик у нас пекут каждое утро. Ждём Вас снова.",
        "Мария, спасибо! Ждём снова.",
        "Здравствуйте! Нам очень жаль, что заказ задержался. Разберёмся.",
    ])
    drafts._client = fake

    r = Review(source="yandex", branch_name="Капучино",
               branch_address="Волгоград, улица Рокоссовского, 54", rating=5,
               rating_confident=True, text="Очень вкусный медовик", author="Мария",
               reply_url="https://yandex.ru/sprav/123/p/edit/reviews",
               received_at=datetime.now(TZ) - timedelta(hours=1))
    r.dedup_key = make_dedup_key(r)
    rid = storage.save_review(conn, r).review_id

    fb = FakeBot()
    await B.deliver(fb, conn, "-100ALL", rid, "")
    card = fb.log[-1]

    print("\n=== «Подготовить ответ» ===")
    take = FakeCallback(fb, f"rv:take:{rid}", card)
    await B.on_button(take, None, conn)
    cb = FakeCallback(fb, f"rv:draft:{rid}", card)
    await B.on_button(cb, None, conn)
    draft_msg = fb.log[-1]
    ok("Черновик ответа" in draft_msg.text, "пришло сообщение с черновиком")
    ok("<pre>" in draft_msg.text, "текст в блоке для копирования")
    labels = [b.text for row in draft_msg.markup.inline_keyboard for b in row]
    ok({"🔄 Другой вариант", "✂️ Короче", "🤗 Теплее", "👔 Официальнее"} <= set(labels),
       "все четыре варианта из ТЗ")
    ok("Открыть в Яндексе" in labels, "ссылка в кабинет площадки")
    print("\n" + "\n".join("    " + l for l in plain(draft_msg.text).split("\n")))

    print("\n=== «Короче» ===")
    cb2 = FakeCallback(fb, f"dr:shorter:{rid}", draft_msg)
    await draft_ui.on_draft_button(cb2, FakeState(), conn)
    ok("Ждём снова." in draft_msg.text, "черновик переписан на месте")
    ok("Медовик у нас пекут" in fake.calls[-1]["user"], "модели передан прошлый вариант")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM drafts WHERE review_id = %s", (rid,))
        ok(cur.fetchone()["n"] == 2, "оба варианта сохранены в базе")

    print("\n=== «Опубликовал(а) как есть» ===")
    cb3 = FakeCallback(fb, f"dr:done:{rid}", draft_msg)
    await draft_ui.on_draft_button(cb3, FakeState(), conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status, reply_text, assignee_name FROM reviews WHERE id = %s", (rid,))
        row = cur.fetchone()
    ok(row["status"] == "answered", "отзыв закрыт")
    ok(row["reply_text"] == "Мария, спасибо! Ждём снова.", "сохранён последний вариант")
    ok(row["assignee_name"] == "Анна", "ответственный сохранился")
    ok(any("ОТВЕЧЕНО" in plain(t) for _, _, t in fb.edits), "карточка в чате обновилась")

    print("\n=== «Опубликовал(а) со своими правками» ===")
    r2 = Review(source="2gis", branch_name="Капучино",
                branch_address="Волгоград, 30-летия Победы бульвар, 21", rating=2,
                rating_confident=True, text="Ждали заказ 40 минут",
                received_at=datetime.now(TZ) - timedelta(hours=1))
    r2.dedup_key = make_dedup_key(r2)
    rid2 = storage.save_review(conn, r2).review_id
    await B.deliver(fb, conn, "-100ALL", rid2, "")
    card2 = fb.log[-1]
    await B.on_button(FakeCallback(fb, f"rv:take:{rid2}", card2, 2, "Борис"), None, conn)
    await B.on_button(FakeCallback(fb, f"rv:draft:{rid2}", card2, 2, "Борис"), None, conn)
    msg2 = fb.log[-1]
    recent_in_prompt = "Мария, спасибо" in fake.calls[-1]["system"]
    ok(recent_in_prompt, "начало прошлого ответа передано как запрет на повтор")

    st = FakeState()
    await draft_ui.on_draft_button(FakeCallback(fb, f"dr:own:{rid2}", msg2, 2, "Борис"), st, conn)
    ok(st.state == draft_ui.OwnReply.waiting, "бот ждёт итоговый текст")
    own = FakeMsg(fb, -100, "Здравствуйте! Извините за ожидание, мы уже разбираемся с этим.")
    own.from_user = SimpleNamespace(id=2, full_name="Борис", username=None)
    await draft_ui.receive_own_reply(own, st, conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status, reply_text FROM reviews WHERE id = %s", (rid2,))
        row = cur.fetchone()
    ok(row["status"] == "answered", "отзыв закрыт")
    ok(row["reply_text"].startswith("Здравствуйте! Извините"), "сохранён текст сотрудника")

    print("\n=== ответ попадает в Google Таблицу ===")
    import sheets
    rows = sheets.fetch_rows(conn)
    answers = {r[3] or r[2]: r[9] for r in rows}
    ok(any("Ждём снова" in v for v in answers.values()), "колонка «Ответ» заполнена")

    print("\n=== черновики не подключены ===")
    del os.environ["GIGACHAT_CREDENTIALS"]
    cb5 = FakeCallback(fb, f"rv:draft:{rid}", card)
    await B.on_button(cb5, None, conn)
    ok("GIGACHAT_CREDENTIALS" in cb5.alerts[-1], "понятная подсказка администратору")


def main():
    unit_tests()
    conn = storage.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS review_topics, drafts, review_messages, reviews, emails, branches, state CASCADE")
    conn.commit()
    storage.init_schema(conn)
    from seed_branches import BRANCHES, SOURCES
    for source in SOURCES:
        for b in BRANCHES:
            storage.upsert_branch(conn, source, b["name"], b["address"])
    conn.commit()
    asyncio.run(flow(conn))
    conn.close()


if __name__ == "__main__":
    main()
