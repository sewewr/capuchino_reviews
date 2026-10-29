"""
ИИ-аналитика по темам (п. 14 ТЗ).

Что делает:
  — GigaChat разбирает каждый отзыв на темы из фиксированного списка
    и отмечает, хвалят тему или ругают. Разбор хранится в базе, поэтому
    каждый отзыв уходит в модель один раз;
  — по разобранным отзывам считаем, что чаще хвалят и ругают за период,
    и где именно ругают (проблемные зоны по филиалам);
  — по запросу или в еженедельном отчёте ИИ пишет короткое резюме:
    тенденции и проблемные зоны.

Список тем фиксированный: свободные темы модель называла бы по-разному
(«ожидание», «долго ждали», «скорость»), и посчитать их было бы нельзя.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import analytics
from analytics import Period

log = logging.getLogger(__name__)

# ключ → (как показывать, подсказка модели)
TOPICS = {
    "desserts":     ("Десерты и торты", "вкус, свежесть, выбор десертов, тортов, выпечки"),
    "drinks":       ("Кофе и напитки", "кофе, чай, лимонады, коктейли"),
    "breakfast":    ("Завтраки", "завтраки, утреннее меню"),
    "food":         ("Блюда и кухня", "горячие блюда, салаты, порции, вкус еды кроме десертов"),
    "staff":        ("Сотрудники", "вежливость, приветливость, грубость конкретных людей"),
    "service":      ("Обслуживание", "организация, ошибки в заказе, внимание к гостю"),
    "waiting":      ("Время ожидания", "долго ждали заказ, очередь, быстро приготовили"),
    "temperature":  ("Температура блюд", "холодный или горячий кофе, остывшая еда"),
    "availability": ("Отсутствие позиций", "чего-то не было в наличии, закончилось"),
    "interior":     ("Интерьер и атмосфера", "уют, дизайн, музыка, шум"),
    "cleanliness":  ("Чистота", "чистота зала, посуды, туалета"),
    "prices":       ("Цены", "дорого, недорого, соотношение цены и качества"),
    "delivery":     ("Доставка и заказ с собой", "доставка, упаковка, заказ навынос"),
}

BATCH = 10           # отзывов в одном запросе к модели
MAX_TEXT = 700       # длинный отзыв обрезаем: для тем хватает

# Те же правила, что в статистике: учебные, дубли и реклама не считаются
_COUNTED = analytics._COUNTED


def label(topic: str) -> str:
    return TOPICS.get(topic, (topic, ""))[0]


# --- Разбор отзывов моделью ---------------------------------------------------


def classify_prompt() -> str:
    lines = [
        "Ты разбираешь отзывы гостей сети кафе-кондитерских на темы.",
        "Темы (ключ — что сюда относится):",
    ]
    lines += [f"{key} — {hint}" for key, (_, hint) in TOPICS.items()]
    lines += [
        "",
        "Для каждого отзыва укажи темы, которые гость ЯВНО упоминает, и знак:",
        "+ если хвалит, - если ругает. Одна тема — один знак: если о теме и",
        "хорошо, и плохо, выбери то, что звучит сильнее.",
        "Не додумывай: «всё вкусно» — это food или desserts, только если ясно,",
        "о чём речь; общая похвала без деталей тем не даёт.",
        "Отзыв без текста или без конкретики — пустой список.",
        "",
        "Ответ — только JSON без пояснений: номер отзыва → список пар [тема, знак].",
        'Пример: {"1": [["desserts", "+"], ["waiting", "-"]], "2": []}',
    ]
    return "\n".join(lines)


def batch_prompt(items: list[dict]) -> str:
    parts = []
    for n, item in enumerate(items, start=1):
        text = " ".join((item["text"] or "").split())[:MAX_TEXT]
        stars = f"{item['rating']}★" if item["rating"] else "оценка неизвестна"
        parts.append(f"{n}. ({stars}) {text or '(без текста)'}")
    return "\n".join(parts)


_SIGN = {"+": 1, "-": -1, "−": -1, "–": -1, "плюс": 1, "минус": -1,
         "pos": 1, "neg": -1, "positive": 1, "negative": -1, "1": 1, "-1": -1}


def parse_answer(raw: str, count: int) -> dict[int, list[tuple[str, int]]]:
    """
    Ответ модели → {номер: [(тема, ±1)]}. Модель иногда оборачивает JSON
    в ```, добавляет текст или путает формат пары — терпим всё, что можно
    понять однозначно, остальное отбрасываем.
    """
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("в ответе нет JSON")
    data = json.loads(m.group(0))

    out: dict[int, list[tuple[str, int]]] = {}
    for key, pairs in data.items():
        try:
            n = int(str(key).strip().rstrip("."))
        except ValueError:
            continue
        if not 1 <= n <= count or not isinstance(pairs, list):
            continue
        seen: dict[str, int] = {}
        for pair in pairs:
            if isinstance(pair, dict):
                pair = [pair.get("topic") or pair.get("тема"), pair.get("sign") or pair.get("знак")]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            topic = str(pair[0]).strip().lower()
            sign = _SIGN.get(str(pair[1]).strip().lower())
            if topic in TOPICS and sign and topic not in seen:
                seen[topic] = sign
        out[n] = list(seen.items())
    return out


def pending(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT r.id, r.rating, COALESCE(r.text_full, r.text) AS text
                FROM reviews r
                WHERE r.topics_at IS NULL AND {_COUNTED}
                ORDER BY r.received_at DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    conn.commit()
    return [dict(r) for r in rows]


def save(conn, review_id: int, topics: list[tuple[str, int]]) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM review_topics WHERE review_id = %s", (review_id,))
        for topic, sign in topics:
            cur.execute(
                "INSERT INTO review_topics (review_id, topic, sentiment) VALUES (%s, %s, %s)",
                (review_id, topic, sign),
            )
        cur.execute("UPDATE reviews SET topics_at = now() WHERE id = %s", (review_id,))
    conn.commit()


def process_pending(conn, client=None, max_batches: int = 5) -> int:
    """
    Разобрать неразобранные отзывы, по BATCH за запрос. Возвращает, сколько
    разобрано. Ошибка модели — не повод падать: остаток разберём в следующий раз.
    """
    import drafts

    done = 0
    for _ in range(max_batches):
        items = pending(conn, BATCH)
        if not items:
            break

        # без текста модели нечего разбирать
        empty = [i for i in items if not (i["text"] or "").strip()]
        for item in empty:
            save(conn, item["id"], [])
            done += 1
        items = [i for i in items if (i["text"] or "").strip()]
        if not items:
            continue

        client = client or drafts.get_client()
        raw = drafts._ask(client, classify_prompt(), batch_prompt(items), 0.1,
                          max_tokens=80 * len(items) + 100)
        try:
            result = parse_answer(raw, len(items))
        except ValueError as e:
            log.warning("не разобрал ответ модели про темы: %s — %r", e, raw[:200])
            raise drafts.DraftError("модель вернула непонятный ответ про темы") from e

        for n, item in enumerate(items, start=1):
            # отзыв, пропущенный моделью, считаем разобранным без тем —
            # иначе он будет уходить в модель снова и снова
            save(conn, item["id"], result.get(n, []))
            done += 1
    return done


# --- Подсчёт ------------------------------------------------------------------


@dataclass
class TopicCount:
    topic: str
    plus: int = 0
    minus: int = 0
    branches: list[tuple[str, int]] = field(default_factory=list)  # где ругают


@dataclass
class TopicStats:
    period: Period
    total: int = 0          # отзывов с текстом за период
    classified: int = 0     # из них разобрано
    topics: dict[str, TopicCount] = field(default_factory=dict)

    def praised(self) -> list[TopicCount]:
        return sorted((t for t in self.topics.values() if t.plus),
                      key=lambda t: (-t.plus, t.topic))

    def criticized(self) -> list[TopicCount]:
        return sorted((t for t in self.topics.values() if t.minus),
                      key=lambda t: (-t.minus, t.topic))


def collect(conn, period: Period) -> TopicStats:
    start, end = period.bounds()
    st = TopicStats(period=period)
    where = f"r.received_at >= %s AND r.received_at < %s AND {_COUNTED}"
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT count(*) FILTER (WHERE COALESCE(r.text_full, r.text) <> '') AS total,
                       count(*) FILTER (WHERE r.topics_at IS NOT NULL
                                        AND COALESCE(r.text_full, r.text) <> '') AS classified
                FROM reviews r WHERE {where}""",
            (start, end),
        )
        row = cur.fetchone()
        st.total, st.classified = row["total"], row["classified"]

        cur.execute(
            f"""SELECT t.topic, t.sentiment, count(*) AS n
                FROM review_topics t JOIN reviews r ON r.id = t.review_id
                WHERE {where} GROUP BY t.topic, t.sentiment""",
            (start, end),
        )
        for r in cur.fetchall():
            tc = st.topics.setdefault(r["topic"], TopicCount(r["topic"]))
            if r["sentiment"] > 0:
                tc.plus = r["n"]
            else:
                tc.minus = r["n"]

        cur.execute(
            f"""SELECT t.topic, COALESCE(min(b.address), 'филиал не определён') AS branch,
                       count(*) AS n
                FROM review_topics t JOIN reviews r ON r.id = t.review_id
                LEFT JOIN branches b ON b.id = r.branch_id
                WHERE {where} AND t.sentiment < 0
                GROUP BY t.topic, b.address_norm ORDER BY count(*) DESC""",
            (start, end),
        )
        for r in cur.fetchall():
            st.topics[r["topic"]].branches.append((analytics.short_address(r["branch"]), r["n"]))
    conn.commit()
    return st


def _before(prev: TopicStats | None, topic: str, attr: str) -> int | None:
    """Сколько было в прошлом периоде; None — сравнивать не с чем."""
    if prev is None or not prev.total:
        return None
    tc = prev.topics.get(topic)
    return getattr(tc, attr) if tc else 0


def _delta(cur: int, prev: int | None) -> str:
    if prev is None or prev == cur:
        return ""
    return f" (▲ +{cur - prev})" if cur > prev else f" (▼ −{prev - cur})"


def _where(tc: TopicCount) -> str:
    """Проблемная зона: где ругают, если это не размазано по всей сети."""
    if not tc.branches:
        return ""
    top_branch, n = tc.branches[0]
    if tc.minus >= 2 and n >= 2:
        return f" · чаще всего {html.escape(top_branch)} ({n})"
    return ""


def render(st: TopicStats, prev: TopicStats | None = None, limit: int = 6) -> str:
    head = f"🧭 <b>Темы отзывов — {st.period.title.lower()}: {st.period.label()}</b>"
    if not st.total:
        return f"{head}\n\nОтзывов с текстом за этот период нет."

    lines = [head]
    if st.classified < st.total:
        lines.append(f"Разобрано ИИ: {st.classified} из {st.total} — остальные в очереди")
    lines.append("")

    praised, criticized = st.praised(), st.criticized()
    lines.append("👍 <b>Хвалят</b>")
    if praised:
        for tc in praised[:limit]:
            lines.append(f"{label(tc.topic)} — {tc.plus}"
                         f"{_delta(tc.plus, _before(prev, tc.topic, 'plus'))}")
    else:
        lines.append("конкретных тем нет")

    lines += ["", "👎 <b>Ругают</b>"]
    if criticized:
        for tc in criticized[:limit]:
            lines.append(f"{label(tc.topic)} — {tc.minus}"
                         f"{_delta(tc.minus, _before(prev, tc.topic, 'minus'))}{_where(tc)}")
    else:
        lines.append("жалоб по темам нет 🎉")
    return "\n".join(lines)


def report_block(st: TopicStats, summary_text: str | None = None) -> str:
    """Короткий блок для еженедельного отчёта."""
    if not st.classified:
        return ""
    praised = ", ".join(f"{label(t.topic).lower()} ({t.plus})" for t in st.praised()[:4])
    criticized = ", ".join(f"{label(t.topic).lower()} ({t.minus}){_where(t)}"
                           for t in st.criticized()[:4])
    lines = ["🧭 <b>Темы недели</b>"]
    lines.append(f"👍 Хвалят: {praised or 'конкретных тем нет'}")
    lines.append(f"👎 Ругают: {criticized or 'жалоб по темам нет'}")
    if summary_text:
        lines += ["", f"🧠 {html.escape(summary_text)}"]
    return "\n".join(lines)


# --- Резюме от ИИ ---------------------------------------------------------------


SUMMARY_SYSTEM = (
    "Ты аналитик сети кафе-кондитерских «Капучино» в Волгограде. По сводке отзывов "
    "за период напиши управляющему короткое резюме: 3–5 предложений, без списков "
    "и заголовков. Что получается хорошо, какие проблемы повторяются и где именно "
    "(филиал), что изменилось по сравнению с прошлым периодом, на что обратить "
    "внимание в первую очередь. Опирайся только на данные из сводки, ничего не "
    "выдумывай, цифры не пересказывай подряд. Если данных мало — так и скажи."
)


def summary_input(conn, st: TopicStats, prev: TopicStats | None, quotes: int = 15) -> str:
    """Сводка для модели: цифры по темам и несколько цитат, сначала жалобы."""
    start, end = st.period.bounds()
    s = analytics.collect(conn, st.period)
    lines = [f"Период: {st.period.label()}",
             f"Отзывов: {s.total}, средняя оценка {analytics.num(s.avg)}, "
             f"негативных: {s.negative}"]
    if prev is not None:
        p = analytics.collect(conn, st.period.previous())
        lines.append(f"Прошлый период: отзывов {p.total}, средняя {analytics.num(p.avg)}")
    lines.append("Хвалят: " + (", ".join(f"{label(t.topic)} {t.plus}"
                                         for t in st.praised()) or "ничего конкретного"))
    lines.append("Ругают: " + (", ".join(
        f"{label(t.topic)} {t.minus} ({'; '.join(f'{b} {n}' for b, n in t.branches[:3])})"
        for t in st.criticized()) or "ничего"))
    if prev is not None:
        lines.append("Ругали в прошлом периоде: " + (", ".join(
            f"{label(t.topic)} {t.minus}" for t in prev.criticized()) or "ничего"))

    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT r.rating, COALESCE(r.text_full, r.text) AS text, b.address
                FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
                WHERE r.received_at >= %s AND r.received_at < %s AND {_COUNTED}
                  AND COALESCE(r.text_full, r.text) <> ''
                ORDER BY r.rating NULLS LAST, r.received_at DESC LIMIT %s""",
            (start, end, quotes),
        )
        rows = cur.fetchall()
    conn.commit()
    lines.append("Отзывы (сначала самые низкие оценки):")
    for r in rows:
        text = " ".join(r["text"].split())[:250]
        lines.append(f"— {r['rating'] or '?'}★, {analytics.short_address(r['address'])}: {text}")
    return "\n".join(lines)


def summarize(conn, period: Period, client=None) -> str:
    """Резюме от ИИ. Кэшируем: та же сводка — тот же ответ, без лишних запросов."""
    import hashlib

    import drafts
    import storage

    st = collect(conn, period)
    if not st.classified:
        return "Разобранных отзывов за период пока нет — резюме делать не из чего."
    prev = collect(conn, period.previous())
    text = summary_input(conn, st, prev if prev.total else None)

    key = "topics_summary:" + hashlib.sha1(text.encode()).hexdigest()[:16]
    cached = storage.get_state(conn, key)
    if cached:
        return cached

    client = client or drafts.get_client()
    answer = drafts.clean(drafts._ask(client, SUMMARY_SYSTEM, text, 0.3, max_tokens=500))
    if not answer:
        raise drafts.DraftError("модель вернула пустое резюме")
    storage.set_state(conn, key, answer)
    return answer
