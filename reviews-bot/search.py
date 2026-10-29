"""
Фильтры и поиск по отзывам (п. 11 ТЗ).

Фильтры из ТЗ: все, сегодня, вчера, за неделю, негативные, необработанные,
обработанные, по филиалу, по площадке; плюс поиск по тексту или имени гостя.
Фильтры сочетаются: «негативные за неделю на Рокоссовского в 2ГИС».

Состояние фильтра целиком лежит в callback_data кнопок, поэтому сообщение
со списком работает и после перезапуска бота. Текст поиска в кнопку не
влезает (лимит Telegram 64 байта) — он хранится в базе под коротким ключом.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from analytics import TZ, short_address

PAGE_SIZE = 8

PERIODS = {"a": "Все", "t": "Сегодня", "y": "Вчера", "w": "Неделя"}
STATUSES = {"a": "", "n": "🚨 Негативные", "o": "⏳ Необработанные", "d": "✅ Обработанные"}
SOURCES = {"a": "Все площадки", "y": "Яндекс", "g": "2ГИС"}
SOURCE_CODES = {"y": "yandex", "g": "2gis"}
SOURCE_NAMES = {"yandex": "Яндекс", "2gis": "2ГИС", "tripadvisor": "Tripadvisor"}

STATUS_ICONS = {"new": "🟢", "attention": "🚨", "in_progress": "🟡",
                "answered": "✅", "skipped": "⚪️"}
STATUS_WORDS = {"new": "новый", "attention": "требует внимания", "in_progress": "в работе",
                "answered": "отвечено", "skipped": "закрыт без ответа"}

MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен",
                "окт", "ноя", "дек"]

SEARCH_KEY = "search:"


@dataclass(frozen=True)
class Filter:
    period: str = "a"   # a | t | y | w
    status: str = "a"   # a | n | o | d
    source: str = "a"   # a | y | g
    branch: str = ""    # "" — все, "u" — не определён, иначе id филиала
    page: int = 0
    query: str = ""     # ключ поиска, сам текст — в базе

    def encode(self, prefix: str = "fl") -> str:
        data = f"{prefix}:{self.period}{self.status}{self.source}:{self.branch}:{self.page}:{self.query}"
        assert len(data.encode()) <= 64, data
        return data

    @classmethod
    def decode(cls, data: str) -> "Filter":
        """Кривые данные — не повод падать: вернём фильтр по умолчанию."""
        try:
            _, flags, branch, page, query = data.split(":", 4)
            f = cls(flags[0], flags[1], flags[2], branch, max(0, int(page)), query)
        except (ValueError, IndexError):
            return cls()
        if (f.period not in PERIODS or f.status not in STATUSES or f.source not in SOURCES
                or not (f.branch in ("", "u") or f.branch.isdigit())):
            return cls()
        return f

    def with_(self, **kw) -> "Filter":
        """Любая смена фильтра возвращает на первую страницу."""
        kw.setdefault("page", 0)
        return replace(self, **kw)

    @property
    def is_default(self) -> bool:
        return self == Filter()


# --- Поиск: текст хранится в базе ---------------------------------------------


def normalize(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def save_query(conn, text: str) -> str:
    import storage

    text = " ".join(text.split())[:100]
    key = hashlib.sha1(normalize(text).encode()).hexdigest()[:10]
    storage.set_state(conn, SEARCH_KEY + key, text)
    return key


def load_query(conn, key: str) -> str | None:
    import storage

    return storage.get_state(conn, SEARCH_KEY + key) if key else None


# --- Выборка ------------------------------------------------------------------


@dataclass
class Row:
    review_id: int
    source: str
    rating: int | None
    text: str
    author: str | None
    status: str
    assignee: str | None
    branch: str
    received_at: datetime
    is_negative: bool


@dataclass
class Page:
    rows: list[Row]
    total: int
    filter: Filter
    query_text: str | None

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // PAGE_SIZE))


def _day_bounds(now: datetime, days_back: int, length: int = 1):
    d = now.astimezone(TZ).date() - timedelta(days=days_back)
    start = datetime.combine(d, time(), TZ)
    return start, start + timedelta(days=length)


def fetch(conn, f: Filter, now: datetime | None = None) -> Page:
    now = now or datetime.now(TZ)
    where = ["NOT ('УЧЕБНЫЙ ОТЗЫВ' = ANY(COALESCE(r.warnings, '{}')))"]
    args: list = []

    if f.period == "t":
        start, end = _day_bounds(now, 0)
    elif f.period == "y":
        start, end = _day_bounds(now, 1)
    elif f.period == "w":
        start, end = _day_bounds(now, 6, 7)
    else:
        start = end = None
    if start:
        where.append("r.received_at >= %s AND r.received_at < %s")
        args += [start, end]

    if f.status == "n":
        where.append("r.is_negative")
    elif f.status == "o":
        where.append("r.status IN ('new', 'attention', 'in_progress')")
    elif f.status == "d":
        where.append("r.status IN ('answered', 'skipped')")

    if f.source in SOURCE_CODES:
        where.append("r.source = %s")
        args.append(SOURCE_CODES[f.source])

    if f.branch == "u":
        where.append("r.branch_id IS NULL")
    elif f.branch:
        # филиал заведён на каждой площадке отдельно — берём все его записи
        where.append("r.branch_id IN (SELECT id FROM branches WHERE address_norm = "
                     "(SELECT address_norm FROM branches WHERE id = %s))")
        args.append(int(f.branch))

    query_text = load_query(conn, f.query) if f.query else None
    if query_text:
        pattern = "%" + normalize(query_text).replace("\\", "\\\\").replace(
            "%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "translate(lower(COALESCE(r.text_full, '') || ' ' || r.text || ' ' || "
            "COALESCE(r.author, '')), 'ё', 'е') LIKE %s"
        )
        args.append(pattern)

    sql = f"""
        SELECT r.id, r.source, r.rating, COALESCE(r.text_full, r.text) AS text, r.author,
               r.status, r.assignee_name, r.received_at, r.is_negative,
               COALESCE(b.address, r.branch_address_raw) AS branch,
               count(*) OVER () AS total
        FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
        WHERE {' AND '.join(where)}
        ORDER BY r.received_at DESC, r.id DESC
        LIMIT %s OFFSET %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, args + [PAGE_SIZE, f.page * PAGE_SIZE])
        rows = cur.fetchall()
        total = rows[0]["total"] if rows else 0
        if not rows and f.page > 0:
            # страница опустела (отзывы закрыли) — показываем первую
            return fetch(conn, f.with_(page=0), now)
    conn.commit()

    return Page(
        rows=[Row(r["id"], r["source"], r["rating"], r["text"] or "", r["author"],
                  r["status"], r["assignee_name"], short_address(r["branch"]),
                  r["received_at"], r["is_negative"]) for r in rows],
        total=total, filter=f, query_text=query_text,
    )


def branch_choices(conn) -> list[tuple[str, str]]:
    """Филиалы для выбора: по одному на адрес, хотя заведены на каждой площадке."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(id) AS id, min(address) AS address FROM branches "
            "WHERE active GROUP BY address_norm ORDER BY min(id)"
        )
        rows = cur.fetchall()
    conn.commit()
    return [(str(r["id"]), short_address(r["address"])) for r in rows]


def branch_name(conn, branch: str) -> str:
    if branch == "u":
        return "филиал не определён"
    with conn.cursor() as cur:
        cur.execute("SELECT address FROM branches WHERE id = %s", (int(branch),))
        row = cur.fetchone()
    conn.commit()
    return short_address(row["address"]) if row else "филиал удалён"


# --- Оформление -------------------------------------------------------------


def snippet(text: str, query: str | None, width: int = 90) -> str:
    """Кусок текста; при поиске — вокруг найденного, найденное жирным."""
    text = " ".join(text.split())
    if not text:
        return "<i>без текста</i>"

    pos = normalize(text).find(normalize(query)) if query else -1
    if pos < 0:
        cut = text[:width].rstrip()
        return html.escape(cut) + ("…" if len(text) > width else "")

    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    start = max(0, min(start, end - width))
    piece = text[start:end]
    rel = pos - start
    hit = len(query)
    out = (html.escape(piece[:rel]) + "<b>" + html.escape(piece[rel:rel + hit]) + "</b>"
           + html.escape(piece[rel + hit:]))
    return ("…" if start > 0 else "") + out + ("…" if end < len(text) else "")


def _date(dt: datetime) -> str:
    d = dt.astimezone(TZ)
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"


def describe(f: Filter, conn=None, query_text: str | None = None) -> str:
    parts = []
    if f.period != "a":
        parts.append(PERIODS[f.period].lower() if f.period != "w" else "за неделю")
    if f.status != "a":
        parts.append(STATUSES[f.status].split(" ", 1)[1].lower())
    if f.branch and conn is not None:
        parts.append(branch_name(conn, f.branch))
    if f.source != "a":
        parts.append(SOURCES[f.source])
    if query_text:
        parts.append(f"поиск «{html.escape(query_text)}»")
    return " · ".join(parts) if parts else "все отзывы"


def render(page: Page, conn=None) -> str:
    f = page.filter
    head = f"🔎 <b>Отзывы</b>: {describe(f, conn, page.query_text)}"
    if f.query and page.query_text is None:
        return head + "\n\nПоиск устарел — нажмите «🔍 Поиск» и пришлите слово ещё раз."
    if not page.total:
        return head + "\n\nНичего не нашлось."

    first = f.page * PAGE_SIZE + 1
    last = first + len(page.rows) - 1
    lines = [head, f"Найдено: {page.total}" + (f" · показаны {first}–{last}"
                                               if page.total > PAGE_SIZE else ""), ""]
    for r in page.rows:
        stars = f"{r.rating}★" if r.rating else "?★"
        lines.append(f"<b>#{r.review_id}</b> {stars} · {SOURCE_NAMES.get(r.source, r.source)} · "
                     f"{html.escape(r.branch)} · {_date(r.received_at)}")
        who = f"{html.escape(r.author)}: " if r.author else ""
        state = STATUS_ICONS.get(r.status, "")
        if r.status == "in_progress" and r.assignee:
            state += f" у {html.escape(r.assignee)}"
        lines.append(f"{state} {who}{snippet(r.text, page.query_text)}")
        lines.append("")
    lines.append("Чтобы открыть карточку с кнопками, нажмите её номер.")
    return "\n".join(lines)
