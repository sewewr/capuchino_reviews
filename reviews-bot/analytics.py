"""
Аналитика (п. 12 ТЗ) и еженедельный отчёт (п. 13 ТЗ).

Считаем по базе, Telegram здесь не нужен — только текст сообщений.

Что учитывается:
  — дата отзыва — дата письма от площадки (отстаёт от публикации на 1–2 дня):
    другой даты у Яндекса в письме нет;
  — учебные отзывы (/demo) и возможные дубли в статистику не входят;
  — «негативный» — по порогу из /threshold, как и в остальном боте;
  — «без ответа» — новые, требующие внимания и взятые в работу.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Moscow")

SOURCE_NAMES = {"yandex": "Яндекс", "2gis": "2ГИС", "tripadvisor": "Tripadvisor"}
OPEN_STATUSES = ("new", "attention", "in_progress")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Отзывы, которые в статистику не идут
_COUNTED = ("NOT ('УЧЕБНЫЙ ОТЗЫВ' = ANY(COALESCE(r.warnings, '{}'))) "
            "AND NOT r.possible_duplicate")


# --- Периоды ----------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """Полуинтервал дат [start, end): end — первый день, который уже не входит."""

    start: date
    end: date
    title: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def previous(self) -> "Period":
        """Такой же по длине период прямо перед этим — для сравнения."""
        return Period(self.start - timedelta(days=self.days), self.start, "")

    def bounds(self) -> tuple[datetime, datetime]:
        return (datetime.combine(self.start, time(), TZ),
                datetime.combine(self.end, time(), TZ))

    def label(self) -> str:
        return format_range(self.start, self.end - timedelta(days=1))


def today(now: datetime | None = None) -> date:
    return (now or datetime.now(TZ)).astimezone(TZ).date()


def period_today(now=None) -> Period:
    d = today(now)
    return Period(d, d + timedelta(days=1), "Сегодня")


def period_week(now=None) -> Period:
    """Последние 7 дней, включая сегодня."""
    d = today(now)
    return Period(d - timedelta(days=6), d + timedelta(days=1), "Неделя")


def period_month(now=None) -> Period:
    """Последние 30 дней, включая сегодня."""
    d = today(now)
    return Period(d - timedelta(days=29), d + timedelta(days=1), "Месяц")


def period_report(now=None) -> Period:
    """Для еженедельного отчёта — 7 полных дней до сегодняшнего.
    В понедельник это ровно прошлая неделя, с понедельника по воскресенье."""
    d = today(now)
    return Period(d - timedelta(days=7), d, "Неделя")


_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")


def parse_period(raw: str, now=None) -> Period:
    """
    Свой период: «01.10 15.10», «1.10.2026–15.10.2026», «01.10-15.10».
    Одна дата — один день. Год по умолчанию текущий; дата из будущего
    без года считается прошлогодней.
    """
    d_today = today(now)
    found = _DATE.findall(raw or "")
    if not found or len(found) > 2:
        raise ValueError("нужны одна или две даты, например 01.10 15.10")

    dates = []
    for day, month, year in found:
        y = int(year) if year else d_today.year
        y = y + 2000 if y < 100 else y
        try:
            d = date(y, int(month), int(day))
        except ValueError:
            raise ValueError(f"нет такой даты: {day}.{month}") from None
        if not year and d > d_today:
            d = d.replace(year=d.year - 1)
        dates.append(d)

    start, end = min(dates), max(dates)
    if (end - start).days > 366:
        raise ValueError("период длиннее года — сузьте, пожалуйста")
    return Period(start, end + timedelta(days=1), "Период")


# --- Подсчёт ------------------------------------------------------------------


@dataclass
class BranchStats:
    name: str
    total: int
    avg: float | None
    negative: int
    open: int


@dataclass
class Stats:
    period: Period
    total: int = 0
    avg: float | None = None
    no_rating: int = 0
    stars: dict[int, int] = field(default_factory=lambda: {s: 0 for s in range(1, 6)})
    negative: int = 0
    open: int = 0
    in_progress: int = 0
    by_source: dict[str, tuple[int, float | None]] = field(default_factory=dict)
    by_branch: list[BranchStats] = field(default_factory=list)
    threshold: int = 3

    @property
    def positive(self) -> int:
        return self.stars[4] + self.stars[5]

    @property
    def neutral(self) -> int:
        return self.stars[3]

    @property
    def low(self) -> int:
        return self.stars[1] + self.stars[2]


def collect(conn, period: Period, threshold: int = 3) -> Stats:
    start, end = period.bounds()
    s = Stats(period=period, threshold=threshold)
    where = f"r.received_at >= %s AND r.received_at < %s AND {_COUNTED}"
    args = (start, end)

    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT count(*) AS total, avg(r.rating)::float AS avg,
                       count(*) FILTER (WHERE r.rating IS NULL) AS no_rating,
                       count(*) FILTER (WHERE r.is_negative) AS negative,
                       count(*) FILTER (WHERE r.status IN ('new','attention','in_progress')) AS open,
                       count(*) FILTER (WHERE r.status = 'in_progress') AS in_progress
                FROM reviews r WHERE {where}""",
            args,
        )
        row = cur.fetchone()
        s.total, s.avg, s.no_rating = row["total"], row["avg"], row["no_rating"]
        s.negative, s.open, s.in_progress = row["negative"], row["open"], row["in_progress"]

        cur.execute(
            f"SELECT r.rating, count(*) AS n FROM reviews r "
            f"WHERE {where} AND r.rating BETWEEN 1 AND 5 GROUP BY r.rating",
            args,
        )
        for r in cur.fetchall():
            s.stars[r["rating"]] = r["n"]

        cur.execute(
            f"SELECT r.source, count(*) AS n, avg(r.rating)::float AS avg FROM reviews r "
            f"WHERE {where} GROUP BY r.source ORDER BY count(*) DESC, r.source",
            args,
        )
        s.by_source = {r["source"]: (r["n"], r["avg"]) for r in cur.fetchall()}

        # Один филиал заведён отдельно на каждой площадке — склеиваем по адресу.
        cur.execute(
            f"""SELECT COALESCE(min(b.address), 'филиал не определён') AS name,
                       count(*) AS n, avg(r.rating)::float AS avg,
                       count(*) FILTER (WHERE r.is_negative) AS negative,
                       count(*) FILTER (WHERE r.status IN ('new','attention','in_progress')) AS open
                FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
                WHERE {where}
                GROUP BY b.address_norm
                ORDER BY count(*) DESC, avg(r.rating) DESC NULLS LAST""",
            args,
        )
        s.by_branch = [BranchStats(short_address(r["name"]), r["n"], r["avg"],
                                   r["negative"], r["open"]) for r in cur.fetchall()]
    return s


@dataclass
class AttentionItem:
    review_id: int
    rating: int | None
    branch: str
    text: str
    received_at: datetime
    assignee: str | None
    link: str | None = None


def needs_attention(conn, limit: int = 10) -> tuple[list[AttentionItem], int]:
    """Негативные отзывы без ответа — за всё время, самые старые первыми:
    они ждут дольше всех. Возвращает первые limit и общее число."""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT r.id, r.rating, b.address, r.branch_address_raw, r.text,
                       r.received_at, r.assignee_name, count(*) OVER () AS total
                FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
                WHERE r.is_negative AND r.status IN ('new','attention','in_progress')
                  AND {_COUNTED}
                ORDER BY r.received_at
                LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    items = [AttentionItem(r["id"], r["rating"],
                           short_address(r["address"] or r["branch_address_raw"]),
                           r["text"], r["received_at"], r["assignee_name"]) for r in rows]
    return items, (rows[0]["total"] if rows else 0)


def message_link(chat_id: str, message_id: int) -> str | None:
    """Ссылка на сообщение работает только в супергруппах и каналах (-100…)."""
    chat_id = str(chat_id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def attach_links(conn, items: list[AttentionItem], prefer_chat: str | None = None) -> None:
    """К каждому отзыву — ссылка на его карточку, лучше в том чате, куда идёт отчёт."""
    import storage

    for item in items:
        copies = storage.review_messages(conn, item.review_id)
        copies.sort(key=lambda c: (str(c[0]) != str(prefer_chat or ""), -c[1]))
        for chat_id, message_id in copies:
            link = message_link(chat_id, message_id)
            if link:
                item.link = link
                break


# --- Оформление -------------------------------------------------------------


def short_address(address: str | None) -> str:
    if not address:
        return "филиал не определён"
    parts = [p.strip() for p in address.split(",")]
    parts = [p for p in parts if p.lower() not in ("волгоград", "россия")
             and not p.lower().startswith("район ")]
    return ", ".join(parts) if parts else address


def format_range(first: date, last: date) -> str:
    """«29 октября», «23–29 октября», «28 сентября – 4 октября», с годом, если не текущий."""
    year = "" if last.year == today().year and first.year == last.year else f" {last.year}"
    if first == last:
        return f"{first.day} {MONTHS[first.month - 1]}{year}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {MONTHS[last.month - 1]}{year}"
    first_year = f" {first.year}" if first.year != last.year else ""
    return (f"{first.day} {MONTHS[first.month - 1]}{first_year} – "
            f"{last.day} {MONTHS[last.month - 1]}{year}")


def num(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}".replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def reviews_word(n: int) -> str:
    return plural(n, "отзыв", "отзыва", "отзывов")


def delta_count(cur: int, prev: int) -> str:
    if cur == prev:
        return "столько же, сколько"
    sign = "▲ +" if cur > prev else "▼ −"
    return f"{sign}{abs(cur - prev)} к"


def delta_avg(cur: float | None, prev: float | None) -> str:
    if cur is None or prev is None:
        return ""
    diff = round(cur, 1) - round(prev, 1)
    if abs(diff) < 0.05:
        return " (без изменений)"
    return f" ({'▲ +' if diff > 0 else '▼ −'}{num(abs(diff))})"


def bar(n: int, top: int, width: int = 10) -> str:
    if n == 0 or top == 0:
        return ""
    return "▇" * max(1, round(width * n / top))


def _sources_line(s: Stats) -> str:
    return " · ".join(f"{SOURCE_NAMES.get(src, src)} {n} ({num(avg)}★)"
                      for src, (n, avg) in s.by_source.items())


def _branch_line(b: BranchStats) -> str:
    extra = f" · 🚨{b.negative}" if b.negative else ""
    return f"{html.escape(b.name)} — {b.total} · {num(b.avg)}★{extra}"


def render_stats(s: Stats, prev: Stats | None = None) -> str:
    """Сообщение для /stats (п. 12 ТЗ)."""
    head = f"📊 <b>{s.period.title}: {s.period.label()}</b>"
    if s.total == 0:
        return f"{head}\n\nОтзывов за этот период нет."

    lines = [head, ""]
    total = f"Отзывов: <b>{s.total}</b>"
    if prev is not None and s.total != prev.total:
        total += f" ({delta_count(s.total, prev.total)} предыдущему периоду)"
    elif prev is not None:
        total += " (столько же, сколько в предыдущем периоде)"
    lines.append(total)
    avg = f"Средняя оценка: <b>{num(s.avg)}★</b>"
    if prev is not None:
        avg += delta_avg(s.avg, prev.avg)
    lines.append(avg)
    lines.append(f"Негативных (1–{s.threshold}★): {s.negative} · без ответа: {s.open}"
                 + (f", из них в работе {s.in_progress}" if s.in_progress else ""))

    lines += ["", "<b>По оценкам</b>"]
    top = max(s.stars.values())
    for star in range(5, 0, -1):
        n = s.stars[star]
        lines.append(f"<code>{star}★ {bar(n, top):<10} </code>{n}")
    if s.no_rating:
        lines.append(f"без оценки: {s.no_rating}")

    lines += ["", "<b>По площадкам</b>", _sources_line(s)]

    lines += ["", "<b>По филиалам</b>"]
    lines += [_branch_line(b) for b in s.by_branch]
    return "\n".join(lines)


def _attention_line(item: AttentionItem, now: datetime) -> str:
    stars = f"{item.rating}★" if item.rating else "?★"
    text = " ".join(item.text.split())
    text = text[:60].rstrip() + "…" if len(text) > 60 else text
    text = html.escape(text) if text else "без текста"
    days = (now.date() - item.received_at.astimezone(TZ).date()).days
    waiting = "сегодня" if days <= 0 else f"ждёт {days} {plural(days, 'день', 'дня', 'дней')}"
    who = f" · в работе у {html.escape(item.assignee)}" if item.assignee else ""
    title = f"{stars} {html.escape(item.branch)}"
    if item.link:
        title = f'<a href="{item.link}">{title}</a>'
    return f"• {title} — «{text}» · {waiting}{who}"


def render_weekly(s: Stats, prev: Stats, attention: list[AttentionItem],
                  attention_total: int, now: datetime | None = None) -> str:
    """Еженедельный отчёт (п. 13 ТЗ)."""
    now = now or datetime.now(TZ)
    lines = [f"🗓 <b>Отзывы за неделю: {s.period.label()}</b>", ""]

    if s.total == 0:
        lines.append("За неделю отзывов не было.")
        if prev.total:
            lines.append(f"Неделей раньше было {prev.total} — стоит проверить, "
                         "приходят ли письма от площадок.")
    else:
        if s.total != prev.total:
            lines.append(f"Отзывов: <b>{s.total}</b> ({delta_count(s.total, prev.total)} "
                         "прошлой неделе)")
        else:
            lines.append(f"Отзывов: <b>{s.total}</b> (столько же, сколько неделей раньше)")
        lines.append(f"Средняя оценка: <b>{num(s.avg)}★</b>{delta_avg(s.avg, prev.avg)}")
        lines.append(f"Площадки: {_sources_line(s)}")
        lines.append(f"👍 4–5★: {s.positive} · 😐 3★: {s.neutral} · 👎 1–2★: {s.low}"
                     + (f" · без оценки: {s.no_rating}" if s.no_rating else ""))

        leader = s.by_branch[0]
        tie = [b for b in s.by_branch if b.total == leader.total]
        if len(tie) == 1:
            lines.append(f"🏆 Больше всего отзывов: {html.escape(leader.name)} — "
                         f"{leader.total} ({num(leader.avg)}★)")
        else:
            names = ", ".join(html.escape(b.name) for b in tie[:3])
            lines.append(f"🏆 Больше всего отзывов ({leader.total}): {names}")

        worst = [b for b in s.by_branch if b.negative]
        if worst:
            worst.sort(key=lambda b: (-b.negative, b.avg or 5))
            lines.append("⚠️ Больше всего негатива: " + ", ".join(
                f"{html.escape(b.name)} ({b.negative})" for b in worst[:3]))

        lines += ["", "<b>По филиалам</b>"]
        lines += [_branch_line(b) for b in s.by_branch]

    lines.append("")
    if attention_total:
        lines.append(f"🚨 <b>Требуют внимания: {attention_total}</b> "
                     f"(негатив без ответа, за всё время)")
        lines += [_attention_line(i, now) for i in attention]
        if attention_total > len(attention):
            lines.append(f"…и ещё {attention_total - len(attention)} — /negative")
    else:
        lines.append("✅ Негативных отзывов без ответа нет.")
    return "\n".join(lines)


# --- Расписание отчёта ------------------------------------------------------

REPORT_ENABLED = "report_enabled"
REPORT_DAY = "report_day"
REPORT_TIME = "report_time"
REPORT_CHAT = "report_chat"
REPORT_LAST = "report_last"

DEFAULT_DAY = 0  # понедельник
DEFAULT_TIME = "10:00"


@dataclass
class Schedule:
    enabled: bool
    day: int
    at: time
    chat: str | None

    def describe(self) -> str:
        if not self.enabled:
            return "еженедельный отчёт выключен"
        return f"{EVERY_DAY[self.day]} в {self.at:%H:%M} (МСК)"


EVERY_DAY = ["каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг",
             "каждую пятницу", "каждую субботу", "каждое воскресенье"]


def load_schedule(conn, default_chat: str | None = None) -> Schedule:
    import storage

    enabled = (storage.get_state(conn, REPORT_ENABLED) or "1") == "1"
    try:
        day = int(storage.get_state(conn, REPORT_DAY) or DEFAULT_DAY) % 7
    except ValueError:
        day = DEFAULT_DAY
    try:
        h, m = (storage.get_state(conn, REPORT_TIME) or DEFAULT_TIME).split(":")
        at = time(int(h), int(m))
    except ValueError:
        at = time(10, 0)
    chat = storage.get_state(conn, REPORT_CHAT) or default_chat or None
    return Schedule(enabled, day, at, chat)


_DAY_WORDS = {
    0: ("пн", "понедельник"), 1: ("вт", "вторник"), 2: ("ср", "среда", "среду"),
    3: ("чт", "четверг"), 4: ("пт", "пятница", "пятницу"),
    5: ("сб", "суббота", "субботу"), 6: ("вс", "воскресенье"),
}


def parse_schedule(raw: str) -> tuple[int | None, time | None, bool | None]:
    """
    «пн 10:00», «пятница 18:30», «9:00», «выкл», «вкл».
    Возвращает (день, время, включён) — None там, где ничего не задано.
    """
    text = (raw or "").strip().lower()
    if text in ("off", "выкл", "выключить", "нет", "0"):
        return None, None, False
    if text in ("on", "вкл", "включить", "да"):
        return None, None, True

    day = None
    for d, words in _DAY_WORDS.items():
        if any(re.search(rf"(?<!\w){w}(?!\w)", text) for w in words):
            day = d
            break

    at = None
    m = re.search(r"(\d{1,2})[:.](\d{2})", text) or re.search(r"(?<!\d)(\d{1,2})(?!\d)", text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError("время должно быть от 00:00 до 23:59")
        at = time(h, mi)

    if day is None and at is None:
        raise ValueError("не понял. Пример: пн 10:00")
    return day, at, True


def week_key(now: datetime) -> str:
    y, w, _ = now.astimezone(TZ).isocalendar()
    return f"{y}-W{w:02d}"


def report_due(schedule: Schedule, last_sent: str | None, now: datetime) -> bool:
    """
    Пора ли отправлять. Если бот лежал в назначенное время, отчёт уйдёт,
    как только поднимется, — но только в тот же день, чтобы не прислать
    устаревший во вторник. Раз в неделю, повтор после перезапуска не уходит.
    """
    local = now.astimezone(TZ)
    return (schedule.enabled and schedule.chat is not None
            and local.weekday() == schedule.day
            and local.time() >= schedule.at
            and last_sent != week_key(local))


def _reading(conn, fn):
    """Только чтение, но транзакцию закрываем: после ошибки соединение
    иначе останется в «aborted»-состоянии и сломает следующие запросы."""
    try:
        result = fn()
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return result


def build_report(conn, now: datetime | None = None, chat: str | None = None) -> str:
    import storage

    now = now or datetime.now(TZ)

    def run():
        threshold = storage.negative_threshold(conn)
        period = period_report(now)
        cur = collect(conn, period, threshold)
        prev = collect(conn, period.previous(), threshold)
        items, total = needs_attention(conn)
        attach_links(conn, items, chat)
        return render_weekly(cur, prev, items, total, now)

    return _reading(conn, run)


def build_stats(conn, period: Period) -> str:
    import storage

    def run():
        threshold = storage.negative_threshold(conn)
        cur = collect(conn, period, threshold)
        prev = collect(conn, period.previous(), threshold)
        return render_stats(cur, prev)

    return _reading(conn, run)
