"""
Парсер писем-уведомлений «2ГИС для бизнеса» о новых отзывах.

Проверено на реальном письме от noreply@account.2gis.com (20.09.2026).

Что письмо даёт (в отличие от Яндекса):
  + имя автора
  + категорию отзыва прямо в теме: «оставили хороший отзыв»
  + ссылку на отзыв, которую видно без перехода: она закодирована в параметре
    трекинговой ссылки, раскодируется локально, клика по ней не происходит

Чего нет:
  — даты публикации отзыва (только дата письма)
  — полного текста наверняка: рядом всегда кнопка «Читать полностью»,
    на каком объёме 2ГИС режет текст — пока неизвестно

Поля разбираются из текстовой части письма (она у 2ГИС чистая и стабильная),
а звёзды — из HTML, потому что в тексте их нет.
"""

from __future__ import annotations

import base64
import email
import logging
import re
import urllib.parse
from datetime import datetime, timezone
from email import policy
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from parsers.base import ParseError, Review, make_dedup_key

log = logging.getLogger(__name__)

SOURCE = "2gis"
SENDER = "account.2gis.com"

# «О компании Капучино написали хороший отзыв»
HEADER_RE = re.compile(r"О компании\s+(.+?)\s+написали\s+(\S+)\s+отзыв", re.S)
READ_MORE = "Читать полностью"

# Звезда в вёрстке письма: картинка 16×15
STAR_WIDTH = "16px"
STAR_HEIGHT = "15px"

# Категория из темы письма → каким оценкам она соответствует.
# «хороший» подтверждён на реальном письме; остальные слова — предположение,
# уточним на письмах с низкими оценками. Незнакомое слово на оценку не влияет.
CATEGORY_RATINGS = {
    "хороший": {4, 5},
    "отличный": {5},
    "нейтральный": {3},
    "средний": {3},
    "плохой": {1, 2},
    "негативный": {1, 2},
}


def matches(msg_from: str, subject: str = "") -> bool:
    return SENDER in (msg_from or "")


def parse(raw: bytes) -> list[Review]:
    msg = email.message_from_bytes(raw, policy=policy.default)

    sender = str(msg.get("From", ""))
    if SENDER not in sender:
        raise ParseError(f"письмо не от 2ГИС: {sender!r}")

    plain, html = _get_parts(msg)
    if not plain and not html:
        raise ParseError("в письме нет ни текстовой, ни HTML-части")

    if not plain:
        # запасной путь: достаём текст из HTML
        plain = BeautifulSoup(html, "html.parser").get_text("\n")

    reviews = _extract_from_plain(plain)
    if not reviews:
        raise ParseError("в письме не найдено ни одного отзыва")

    stars = _count_stars(html) if html else []
    received_at = _get_date(msg)
    message_id = str(msg.get("Message-ID", "")) or None

    for i, r in enumerate(reviews):
        count = stars[i] if i < len(stars) else None
        _apply_rating(r, count)
        r.received_at = received_at
        r.message_id = message_id
        r.dedup_key = make_dedup_key(r)

    return reviews


# --- Разбор текстовой части ------------------------------------------------


def _get_parts(msg) -> tuple[str | None, str | None]:
    plain = html = None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain" and plain is None:
            plain = part.get_content()
        elif ct == "text/html" and html is None:
            html = part.get_content()
    return plain, html


def _clean_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def _extract_from_plain(text: str) -> list[Review]:
    """
    Структура текстовой части письма:

        О компании Капучино написали хороший отзыв
        Волгоград, 30-летия Победы бульвар, 21
        Артём Науменко
        Все круто
        Читать полностью (https://...)

    Если в письме окажется несколько отзывов, блок «адрес — автор — текст —
    Читать полностью» повторится; разбираем каждый.
    """
    lines = _clean_lines(text)
    joined = "\n".join(lines)

    header = HEADER_RE.search(joined)
    if not header:
        return []
    company = header.group(1).strip()
    category = header.group(2).strip().lower()

    # всё, что после заголовка
    start = next(
        (i for i, l in enumerate(lines) if "написали" in l and "отзыв" in l), None
    )
    if start is None:
        return []

    reviews: list[Review] = []
    block: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith(READ_MORE):
            review = _block_to_review(block, company, category, _extract_url(line))
            if review:
                reviews.append(review)
            block = []
            continue
        block.append(line)

    return reviews


def _block_to_review(
    block: list[str], company: str, category: str, url: str | None
) -> Review | None:
    """Блок: адрес, автор, затем одна или несколько строк текста."""
    if len(block) < 2:
        return None

    review = Review(source=SOURCE, branch_name=company)
    review.branch_address = block[0]

    if len(block) == 2:
        # автора нет, только текст — или отзыв без текста; считаем второе текстом
        review.text = block[1]
        review.warnings.append("автор не найден в письме")
    else:
        review.author = block[1]
        review.text = "\n".join(block[2:]).strip()

    review.reply_url = url
    review.warnings.extend(_category_note(category))
    review._category = category  # временно, для сверки с числом звёзд

    if review.text.endswith(("...", "…")):
        review.text_truncated = True
        review.warnings.append("текст обрезан 2ГИС — нужен добор")

    return review


def _extract_url(line: str) -> str | None:
    """
    «Читать полностью (https://sender.2gis.com/...&url=<base64>&...)»

    Настоящая ссылка закодирована в параметре url. Раскодируем её локально,
    без перехода — так 2ГИС не засчитывает клик.
    """
    m = re.search(r"\((https?://[^)]+)\)", line)
    if not m:
        return None
    tracked = m.group(1)
    return decode_tracked_url(tracked) or tracked


def decode_tracked_url(url: str) -> str | None:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        encoded = query.get("url", [None])[0]
        if not encoded:
            return None
        # 2ГИС кодирует base64 в «URL-безопасном» варианте с ~ вместо =
        s = encoded.replace("~", "=").replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        decoded = base64.b64decode(s).decode("utf-8")
        return decoded if decoded.startswith("http") else None
    except Exception:  # битая ссылка не должна ронять разбор
        log.debug("не раскодировал ссылку 2ГИС: %s", url[:80])
        return None


def _category_note(category: str) -> list[str]:
    if category not in CATEGORY_RATINGS:
        return [f"неизвестная категория в теме письма: «{category}»"]
    return []


# --- Оценка ----------------------------------------------------------------


def _count_stars(html: str) -> list[int]:
    """
    Количество заполненных звёзд для каждого отзыва в порядке появления.

    Звёзды одного отзыва идут подряд; разрыв означает следующий отзыв.
    Заполненными считаются звёзды с той же картинкой, что у первой в ряду:
    если 2ГИС рисует неполный рейтинг серыми звёздами того же размера,
    они в оценку не попадут. На пятёрке все картинки одинаковые — проверено.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[list[str]] = []
    current: list[str] = []
    for img in soup.find_all("img"):
        is_star = img.get("width") == STAR_WIDTH and img.get("height") == STAR_HEIGHT
        if is_star:
            current.append(img.get("src", ""))
        elif current:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    counts = []
    for row in rows:
        filled_src = row[0]
        counts.append(sum(1 for src in row if src == filled_src))
    return counts


def _apply_rating(review: Review, stars: int | None) -> None:
    """
    Оценка = число звёзд. Уверенность — если число сходится с категорией
    из темы письма («хороший» → 4–5). Так у 2ГИС есть два независимых
    источника оценки, и они проверяют друг друга.
    """
    category = getattr(review, "_category", None)
    allowed = CATEGORY_RATINGS.get(category or "")

    if stars is None or not (1 <= stars <= 5):
        review.rating = None
        review.rating_confident = False
        review.warnings.append("звёзды не найдены — оценка неизвестна")
    elif allowed is None:
        review.rating = stars
        review.rating_confident = False
        review.warnings.append("оценку не с чем сверить — проверьте вручную")
    elif stars in allowed:
        review.rating = stars
        review.rating_confident = True
    else:
        review.rating = stars
        review.rating_confident = False
        review.warnings.append(
            f"звёзд {stars}, а в теме «{category}» отзыв — проверьте оценку"
        )

    if hasattr(review, "_category"):
        del review._category


def _get_date(msg) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            log.warning("не разобрал дату письма: %r", raw)
    return datetime.now(timezone.utc)
