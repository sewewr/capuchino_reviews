"""
Третий этап ТЗ: сравнение филиалов, динамика рейтингов и ежемесячный
управленческий отчёт. Причины негатива и положительные темы берутся из
ИИ-разбора тем (topics.py).

Здесь только расчёты и тексты, Telegram — в telegram_ui/manage_ui.py.
"""

from __future__ import annotations

import html
import io
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import analytics
from analytics import TZ, Period, num, short_address

_COUNTED = analytics._COUNTED

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен",
                "окт", "ноя", "дек"]


# --- Календарные месяцы -----------------------------------------------------


def month_period(year: int, month: int) -> Period:
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return Period(start, end, f"{MONTHS_NOM[month - 1].capitalize()} {year}")


def previous_month(p: Period) -> Period:
    last = p.start - timedelta(days=1)
    return month_period(last.year, last.month)


def last_full_month(now: datetime | None = None) -> Period:
    d = analytics.today(now)
    return previous_month(month_period(d.year, d.month))


def this_month_so_far(now: datetime | None = None) -> Period:
    d = analytics.today(now)
    p = month_period(d.year, d.month)
    return Period(p.start, d + timedelta(days=1), p.title + " (по сегодня)")


# --- Сравнение филиалов -------------------------------------------------------


@dataclass
class BranchRow:
    key: str                 # address_norm — один филиал на всех площадках
    name: str
    total: int = 0
    avg: float | None = None
    prev_avg: float | None = None
    negative: int = 0
    closed: int = 0          # отвечено или закрыто без ответа
    reaction_hours: float | None = None  # медиана: от письма до закрытия
    top_complaint: str | None = None

    @property
    def negative_share(self) -> float:
        return self.negative / self.total if self.total else 0.0

    @property
    def closed_share(self) -> float:
        return self.closed / self.total if self.total else 0.0


@dataclass
class Comparison:
    period: Period
    rows: list[BranchRow]
    network: BranchRow


def _reaction(conn, where: str, args: tuple, group: bool) -> dict:
    """Медиана часов от письма до закрытия — считаем в Python, без хитрого SQL."""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT b.address_norm AS key,
                       EXTRACT(EPOCH FROM (r.closed_at - r.received_at)) / 3600 AS h
                FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
                WHERE {where} AND r.closed_at IS NOT NULL""",
            args,
        )
        rows = cur.fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["key"] if group else "*", []).append(max(0.0, float(r["h"])))
    return {k: statistics.median(v) for k, v in out.items()}


def compare_branches(conn, period: Period, prev: Period | None = None) -> Comparison:
    start, end = period.bounds()
    where = f"r.received_at >= %s AND r.received_at < %s AND {_COUNTED}"
    args = (start, end)
    prev = prev or period.previous()

    with conn.cursor() as cur:
        # все филиалы — и те, где отзывов не было: в сравнении это тоже факт
        cur.execute("SELECT address_norm AS key, min(address) AS name FROM branches "
                    "WHERE active GROUP BY address_norm ORDER BY min(id)")
        rows = {r["key"]: BranchRow(r["key"], short_address(r["name"])) for r in cur.fetchall()}

        cur.execute(
            f"""SELECT b.address_norm AS key, count(*) AS n, avg(r.rating)::float AS avg,
                       count(*) FILTER (WHERE r.is_negative) AS neg,
                       count(*) FILTER (WHERE r.status IN ('answered', 'skipped')) AS closed
                FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
                WHERE {where} GROUP BY b.address_norm""",
            args,
        )
        for r in cur.fetchall():
            row = rows.setdefault(r["key"], BranchRow(r["key"], "филиал не определён"))
            row.total, row.avg, row.negative, row.closed = r["n"], r["avg"], r["neg"], r["closed"]

        p_start, p_end = prev.bounds()
        cur.execute(
            f"""SELECT b.address_norm AS key, avg(r.rating)::float AS avg
                FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
                WHERE r.received_at >= %s AND r.received_at < %s AND {_COUNTED}
                GROUP BY b.address_norm""",
            (p_start, p_end),
        )
        for r in cur.fetchall():
            if r["key"] in rows:
                rows[r["key"]].prev_avg = r["avg"]

        cur.execute(
            f"""SELECT b.address_norm AS key, t.topic, count(*) AS n
                FROM review_topics t JOIN reviews r ON r.id = t.review_id
                LEFT JOIN branches b ON b.id = r.branch_id
                WHERE {where} AND t.sentiment < 0
                GROUP BY b.address_norm, t.topic ORDER BY count(*) DESC, t.topic""",
            args,
        )
        for r in cur.fetchall():
            row = rows.get(r["key"])
            if row and row.top_complaint is None:
                row.top_complaint = r["topic"]

        cur.execute(
            f"""SELECT count(*) AS n, avg(r.rating)::float AS avg,
                       count(*) FILTER (WHERE r.is_negative) AS neg,
                       count(*) FILTER (WHERE r.status IN ('answered', 'skipped')) AS closed
                FROM reviews r WHERE {where}""",
            args,
        )
        n = cur.fetchone()
        cur.execute(f"SELECT avg(r.rating)::float AS avg FROM reviews r WHERE "
                    f"r.received_at >= %s AND r.received_at < %s AND {_COUNTED}", (p_start, p_end))
        network_prev = cur.fetchone()["avg"]

    for key, hours in _reaction(conn, where, args, group=True).items():
        if key in rows:
            rows[key].reaction_hours = hours
    network = BranchRow("*", "Вся сеть", n["n"], n["avg"], network_prev, n["neg"], n["closed"],
                        _reaction(conn, where, args, group=False).get("*"))
    conn.commit()

    # Сначала с отзывами — по средней оценке, при равенстве больше отзывов выше
    ordered = sorted(rows.values(), key=lambda b: (b.total == 0, -(b.avg or 0), -b.total, b.name))
    return Comparison(period, ordered, network)


def _hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return "< 1 ч"
    if h < 48:
        return f"{round(h)} ч"
    return f"{round(h / 24)} дн"


def _avg_delta(cur: float | None, prev: float | None) -> str:
    if cur is None or prev is None:
        return ""
    d = round(cur, 1) - round(prev, 1)
    if abs(d) < 0.05:
        return " (=)"
    return f" ({'▲' if d > 0 else '▼'}{num(abs(d))})"


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def render_comparison(c: Comparison, title: str | None = None) -> str:
    import topics

    lines = [f"🏆 <b>{title or 'Сравнение филиалов'}: {c.period.label()}</b>", ""]
    if not c.network.total:
        return lines[0] + "\n\nОтзывов за этот период нет."

    with_reviews = [r for r in c.rows if r.total]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(with_reviews):
        mark = medals[i] if i < 3 and len(with_reviews) > 3 else f"{i + 1}."
        lines.append(f"{mark} <b>{html.escape(r.name)}</b> — {num(r.avg)}★{_avg_delta(r.avg, r.prev_avg)}")
        details = [f"{r.total} отз."]
        if r.negative:
            details.append(f"негатив {_pct(r.negative_share)}")
        details.append(f"закрыто {_pct(r.closed_share)}")
        if r.reaction_hours is not None:
            details.append(f"⏱ {_hours(r.reaction_hours)}")
        if r.top_complaint:
            details.append(f"жалобы: {topics.label(r.top_complaint).lower()}")
        lines.append("    " + " · ".join(details))

    silent = [r.name for r in c.rows if not r.total]
    if silent:
        lines += ["", "Без отзывов: " + ", ".join(html.escape(n) for n in silent)]

    n = c.network
    lines += ["", f"<b>Вся сеть</b> — {num(n.avg)}★{_avg_delta(n.avg, n.prev_avg)} · {n.total} отз. · "
              f"негатив {_pct(n.negative_share)} · закрыто {_pct(n.closed_share)}"
              + (f" · ⏱ {_hours(n.reaction_hours)}" if n.reaction_hours is not None else "")]
    lines.append("<i>▲▼ — к прошлому периоду; ⏱ — обычное время от письма до закрытия отзыва</i>")
    return "\n".join(lines)


# --- Динамика рейтингов ---------------------------------------------------------


@dataclass
class Point:
    label: str
    start: date
    total: int
    avg: float | None
    partial: bool = False   # текущая неделя или месяц — ещё не закончились


@dataclass
class Series:
    title: str
    points: list[Point] = field(default_factory=list)


def _weeks(now: datetime, count: int) -> list[Period]:
    d = analytics.today(now)
    monday = d - timedelta(days=d.weekday())
    return [Period(monday - timedelta(weeks=i), monday - timedelta(weeks=i - 1), "")
            for i in range(count - 1, -1, -1)]


def _months(now: datetime, count: int) -> list[Period]:
    d = analytics.today(now)
    p = month_period(d.year, d.month)
    out = [p]
    for _ in range(count - 1):
        out.insert(0, previous_month(out[0]))
    return out


def dynamics(conn, by: str = "week", branch: str | None = None, now: datetime | None = None,
             count: int | None = None) -> Series:
    """Средняя оценка и число отзывов по неделям (12) или месяцам (6)."""
    now = now or datetime.now(TZ)
    periods = _weeks(now, count or 12) if by == "week" else _months(now, count or 6)
    branch_sql, branch_args, title = "", (), "Вся сеть"
    if branch:
        branch_sql = ("AND r.branch_id IN (SELECT id FROM branches WHERE address_norm = "
                      "(SELECT address_norm FROM branches WHERE id = %s))")
        branch_args = (int(branch),)
        with conn.cursor() as cur:
            cur.execute("SELECT address FROM branches WHERE id = %s", (int(branch),))
            row = cur.fetchone()
        title = short_address(row["address"]) if row else "филиал"

    series = Series(title)
    today = analytics.today(now)
    with conn.cursor() as cur:
        for p in periods:
            start, end = p.bounds()
            cur.execute(
                f"""SELECT count(*) AS n, avg(r.rating)::float AS avg FROM reviews r
                    WHERE r.received_at >= %s AND r.received_at < %s AND {_COUNTED} {branch_sql}""",
                (start, end, *branch_args),
            )
            r = cur.fetchone()
            label = (f"{p.start.day:02d}.{p.start.month:02d}" if by == "week"
                     else MONTHS_SHORT[p.start.month - 1])
            series.points.append(Point(label, p.start, r["n"], r["avg"], partial=p.end > today))
    conn.commit()
    return series


def describe_trend(s: Series, by: str) -> str:
    unit = "недель" if by == "week" else "месяцев"
    # Текущий период ещё не закончился: пара отзывов за два дня не должна
    # решать, «выросла» оценка или «снизилась». Тренд — по завершённым.
    rated = [p for p in s.points if p.avg is not None and not p.partial]
    total = sum(p.total for p in s.points)
    head = f"📈 <b>Динамика — {html.escape(s.title)}</b>, последние {len(s.points)} {unit}"
    if len(rated) < 2:
        return (head + f"\n\nОтзывов за это время: {total}. Для динамики нужно хотя бы два "
                "периода с отзывами — график станет содержательным по мере накопления.")
    first, last = rated[0], rated[-1]
    diff = round(last.avg, 1) - round(first.avg, 1)
    if abs(diff) < 0.05:
        trend = f"средняя оценка держится на уровне {num(last.avg)}★"
    else:
        trend = (f"средняя оценка {'выросла' if diff > 0 else 'снизилась'} с {num(first.avg)}★ "
                 f"до {num(last.avg)}★")
    best = max(rated, key=lambda p: (p.avg, p.total))
    worst = min(rated, key=lambda p: (p.avg, -p.total))
    lines = [head, "", f"С {first.label} по {last.label} {trend}.",
             f"Отзывов за всё время на графике: {total}."]
    if s.points and s.points[-1].partial:
        lines.append(("Текущий месяц" if by == "month" else "Текущая неделя")
                     + " ещё идёт — на графике заштрихован и в тренд не входит.")
    if best is not worst:
        lines.append(f"Лучший период — {best.label} ({num(best.avg)}★), "
                     f"худший — {worst.label} ({num(worst.avg)}★).")
    return "\n".join(lines)


def chart_png(s: Series, by: str) -> bytes:
    """Столбцы — число отзывов, линия — средняя оценка."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [p.label for p in s.points]
    counts = [p.total for p in s.points]
    avgs = [p.avg for p in s.points]

    fig, ax1 = plt.subplots(figsize=(8, 4.2), dpi=150)
    colors = ["#E6EDF5" if p.partial else "#C9D7E8" for p in s.points]
    ax1.bar(range(len(labels)), counts, color=colors, width=0.6, label="Отзывов", zorder=2)
    for i, p in enumerate(s.points):
        if p.partial:
            ax1.bar(i, counts[i], color="none", edgecolor="#9DB2CC", hatch="///", width=0.6,
                    linewidth=0, zorder=2)
    ax1.set_ylabel("Отзывов", color="#5B6B7F")
    ax1.tick_params(axis="y", colors="#5B6B7F")
    ax1.set_xticks(range(len(labels)), labels, fontsize=8, color="#333333")
    ax1.set_ylim(0, max(counts + [1]) * 1.35)
    from matplotlib.ticker import MaxNLocator

    ax1.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax1.grid(axis="y", color="#EEEEEE", zorder=0)
    for side in ("top", "right"):
        ax1.spines[side].set_visible(False)

    ax2 = ax1.twinx()
    xs = [i for i, a in enumerate(avgs) if a is not None]
    ys = [a for a in avgs if a is not None]
    done = [(x, y) for x, y in zip(xs, ys) if not s.points[x].partial]
    if done:
        ax2.plot(*zip(*done), color="#1F3A5F", linewidth=2.2, marker="o", markersize=5, zorder=3)
    # незавершённый период — пунктиром от последней завершённой точки
    tail = [(x, y) for x, y in zip(xs, ys) if s.points[x].partial]
    if tail:
        seg = ([done[-1]] if done else []) + tail
        ax2.plot(*zip(*seg), color="#1F3A5F", linewidth=1.6, linestyle="--", zorder=3)
        ax2.plot(*zip(*tail), color="#1F3A5F", linestyle="none", marker="o", markersize=5,
                 markerfacecolor="white", zorder=4)
    for x, y in zip(xs, ys):
        ax2.annotate(num(y), (x, y), textcoords="offset points", xytext=(0, 8),
                     ha="center", fontsize=8, color="#1F3A5F", zorder=4,
                     bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))
    low = min(ys + [5]) if ys else 4
    ax2.set_ylim(max(1, low - 0.6), 5.3)
    ax2.set_ylabel("Средняя оценка, ★", color="#1F3A5F")
    ax2.tick_params(axis="y", colors="#1F3A5F")
    for side in ("top",):
        ax2.spines[side].set_visible(False)

    unit = "по неделям" if by == "week" else "по месяцам"
    ax1.set_title(f"{s.title}: оценка и число отзывов {unit}", fontsize=11, color="#1F3A5F",
                  loc="left", pad=12)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# --- Ежемесячный управленческий отчёт -----------------------------------------


def kpi_lines(cur: analytics.Stats, prev: analytics.Stats, comp: Comparison, prev_comp: Comparison) -> list[str]:
    def delta_int(a, b, unit=""):
        if not b:
            return ""
        return "" if a == b else f" ({'▲' if a > b else '▼'}{abs(a - b)}{unit})"

    def delta_pct(a, b):
        if prev.total == 0:
            return ""
        d = round(a * 100) - round(b * 100)
        return "" if d == 0 else f" ({'▲' if d > 0 else '▼'}{abs(d)} п.п.)"

    n, pn = comp.network, prev_comp.network
    lines = [
        f"Отзывов: <b>{cur.total}</b>{delta_int(cur.total, prev.total)}",
        f"Средняя оценка: <b>{num(cur.avg)}★</b>{_avg_delta(cur.avg, prev.avg) if prev.total else ''}",
        f"Негатив (1–{cur.threshold}★): {_pct(n.negative_share)}{delta_pct(n.negative_share, pn.negative_share)}",
        f"Закрыто отзывов: {_pct(n.closed_share)}{delta_pct(n.closed_share, pn.closed_share)}",
    ]
    if n.reaction_hours is not None:
        lines.append(f"Обычное время реакции: {_hours(n.reaction_hours)}")
    lines.append(f"Площадки: {analytics._sources_line(cur)}")
    return lines


MANAGEMENT_SYSTEM = (
    "Ты аналитик сети кафе-кондитерских «Капучино» в Волгограде и готовишь выводы "
    "для управляющего по итогам месяца. Напиши два коротких абзаца без заголовков и списков: "
    "1) главное за месяц — что улучшилось и что ухудшилось по сравнению с прошлым месяцем, "
    "какие филиалы выделяются в лучшую и худшую сторону и почему (по темам жалоб); "
    "2) три конкретные рекомендации, что сделать в следующем месяце, с привязкой к филиалам "
    "и темам. Опирайся только на данные из сводки, не выдумывай причин и цифр. "
    "Если данных мало — скажи об этом прямо. Не больше 900 знаков."
)


def management_input(conn, period: Period, cur, prev, comp: Comparison) -> str:
    import topics

    st = topics.collect(conn, period)
    pst = topics.collect(conn, previous_month(period) if period.days > 20 else period.previous())
    lines = [f"Период: {period.label()}",
             f"Отзывов {cur.total} (прошлый месяц {prev.total}), средняя {num(cur.avg)} "
             f"(была {num(prev.avg)}), негативных {cur.negative}"]
    lines.append("Филиалы (средняя, отзывов, доля негатива, закрыто, главная жалоба):")
    for r in comp.rows:
        if r.total:
            lines.append(f"— {r.name}: {num(r.avg)} (прошлый {num(r.prev_avg)}), {r.total}, "
                         f"{_pct(r.negative_share)}, {_pct(r.closed_share)}, "
                         f"{topics.label(r.top_complaint) if r.top_complaint else 'нет'}")
    lines.append("Хвалят: " + (", ".join(f"{topics.label(t.topic)} {t.plus}" for t in st.praised()) or "—"))
    lines.append("Ругают: " + (", ".join(
        f"{topics.label(t.topic)} {t.minus} ({'; '.join(f'{b} {n}' for b, n in t.branches[:3])})"
        for t in st.criticized()) or "—"))
    if pst.total:
        lines.append("Ругали в прошлом месяце: " + (", ".join(
            f"{topics.label(t.topic)} {t.minus}" for t in pst.criticized()) or "—"))
    return "\n".join(lines)


def management_summary(conn, period: Period, cur, prev, comp, client=None) -> str:
    import hashlib

    import drafts
    import storage

    text = management_input(conn, period, cur, prev, comp)
    key = "mgmt_summary:" + hashlib.sha1(text.encode()).hexdigest()[:16]
    cached = storage.get_state(conn, key)
    if cached:
        return cached
    client = client or drafts.get_client()
    answer = drafts.clean(drafts._ask(client, MANAGEMENT_SYSTEM, text, 0.3, max_tokens=700))
    if not answer:
        raise drafts.DraftError("модель вернула пустые выводы")
    storage.set_state(conn, key, answer)
    return answer


@dataclass
class MonthlyReport:
    period: Period
    text: str                # основное сообщение
    comparison: str          # сравнение филиалов
    topics_text: str         # причины негатива и что хвалят
    chart: bytes | None      # динамика по месяцам
    chart_caption: str


def build_monthly(conn, period: Period, now: datetime | None = None, ai_client=None,
                  with_ai: bool = True, with_chart: bool = True) -> MonthlyReport:
    import drafts
    import storage
    import topics

    now = now or datetime.now(TZ)
    threshold = storage.negative_threshold(conn)
    prev_period = previous_month(period) if period.start.day == 1 else period.previous()
    cur = analytics.collect(conn, period, threshold)
    prev = analytics.collect(conn, prev_period, threshold)
    comp = compare_branches(conn, period, prev_period)
    prev_comp = compare_branches(conn, prev_period, previous_month(prev_period))

    head = f"📑 <b>Управленческий отчёт: {period.title.lower()}</b>\n{period.label()}"
    if not cur.total:
        text = head + "\n\nОтзывов за этот месяц нет."
    else:
        lines = [head, ""] + kpi_lines(cur, prev, comp, prev_comp)
        summary = None
        if with_ai and (ai_client or drafts.enabled()):
            try:
                summary = management_summary(conn, period, cur, prev, comp, ai_client)
            except Exception as e:
                lines += ["", f"<i>Выводы ИИ не получились: {html.escape(str(e))}</i>"]
        if summary:
            lines += ["", "🧠 <b>Выводы и рекомендации</b>", html.escape(summary)]
        text = "\n".join(lines)

    comparison = render_comparison(comp, "Филиалы")

    st = topics.collect(conn, period)
    pst = topics.collect(conn, prev_period)
    topics_text = topics.render(st, pst).replace("Темы отзывов", "Причины негатива и что хвалят")

    chart, caption = None, ""
    if with_chart:
        series = dynamics(conn, "month", now=now, count=6)
        caption = describe_trend(series, "month")
        if sum(p.total for p in series.points):
            chart = chart_png(series, "month")
    return MonthlyReport(period, text, comparison, topics_text, chart, caption)


# --- Расписание ежемесячного отчёта ---------------------------------------------

MONTHLY_ENABLED = "monthly_enabled"
MONTHLY_TIME = "monthly_time"
MONTHLY_LAST = "monthly_last"
DEFAULT_MONTHLY_TIME = "10:00"


def monthly_schedule(conn) -> tuple[bool, time]:
    import storage

    enabled = (storage.get_state(conn, MONTHLY_ENABLED) or "1") == "1"
    try:
        h, m = (storage.get_state(conn, MONTHLY_TIME) or DEFAULT_MONTHLY_TIME).split(":")
        at = time(int(h), int(m))
    except ValueError:
        at = time(10, 0)
    return enabled, at


def month_key(now: datetime) -> str:
    d = now.astimezone(TZ)
    return f"{d.year}-{d.month:02d}"


def monthly_due(enabled: bool, at: time, chat: str | None, last: str | None, now: datetime) -> bool:
    """1-го числа после назначенного времени, раз в месяц. Пропустили утро — догоняем в тот же день."""
    local = now.astimezone(TZ)
    return (enabled and chat is not None and local.day == 1 and local.time() >= at
            and last != month_key(local))
