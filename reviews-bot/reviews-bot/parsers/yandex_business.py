"""
Парсер писем-уведомлений Яндекс Бизнеса о новых отзывах.

Вход:  сырые байты письма (.eml), как их отдаёт IMAP.
Выход: список объектов Review.

Проверено на реальных письмах от email@business.yandex.ru (авг-сен 2026).

Что письмо даёт и чего в нём нет — см. README.md.
Коротко: нет имени автора, нет даты публикации отзыва, нет ID отзыва,
текст обрезается на 200 символах.
"""

from __future__ import annotations

import email
import logging
import re
from datetime import datetime, timezone
from email import policy
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup, Tag

from parsers.base import ParseError, Review, make_dedup_key

__all__ = ["parse", "matches", "Review", "ParseError", "make_dedup_key"]

log = logging.getLogger(__name__)

# --- Признаки писем Яндекс Бизнеса ------------------------------------------

SENDER = "business.yandex.ru"

# Яндекс Бизнес шлёт на тот же адрес дайджесты, рекламу и сообщения про
# рейтинг. Письма об отзывах узнаём по теме: «У вас новый отзыв для X»,
# «У вас новые отзывы для X».
SUBJECT_RE = re.compile(r"отзыв", re.IGNORECASE)

# Ширина картинки-звезды в вёрстке письма. Заполненные и пустые звёзды
# различаются по src; какой src у пустой — пока неизвестно (не было письма
# с оценкой ниже 5). См. _extract_rating.
STAR_IMG_WIDTH = "10"

# Яндекс обрезает текст отзыва на этой длине и дописывает многоточие.
TEXT_LIMIT = 200

REPLY_LABEL = "Ответить"

# Адрес филиала в письме — ссылка без href. Отсюда же начинается карточка отзыва.
ADDRESS_RE = re.compile(
    r"^[А-ЯЁA-Z][^,]{2,40},\s+.{3,80}$"
)

# ID филиала в ссылке кабинета: https://yandex.ru/sprav/187474402700/...
BRANCH_ID_RE = re.compile(r"/sprav/(\d{6,})")


# --- Точка входа ------------------------------------------------------------


def parse(raw: bytes) -> list[Review]:
    """Разобрать письмо Яндекс Бизнеса. Возвращает список отзывов (может быть >1)."""
    msg = email.message_from_bytes(raw, policy=policy.default)

    sender = str(msg.get("From", ""))
    if SENDER not in sender:
        raise ParseError(f"письмо не от Яндекс Бизнеса: {sender!r}")

    html = _get_html(msg)
    if not html:
        raise ParseError("в письме нет HTML-части")

    received_at = _get_date(msg)
    message_id = str(msg.get("Message-ID", "")) or None

    soup = BeautifulSoup(html, "html.parser")
    reviews = _extract_reviews(soup)

    if not reviews:
        raise ParseError("в письме не найдено ни одного отзыва")

    for r in reviews:
        r.received_at = received_at
        r.message_id = message_id
        r.dedup_key = make_dedup_key(r)

    return reviews


def matches(msg_from: str, subject: str = "") -> bool:
    """
    Письмо наше, если оно от Яндекс Бизнеса И похоже на уведомление об отзыве.
    Без проверки темы в парсер летели бы все рассылки подряд.

    Пустая тема — редкость; такое письмо пропускаем в парсер, пусть лучше
    он разберётся, чем мы потеряем настоящий отзыв.
    """
    if SENDER not in (msg_from or ""):
        return False
    return not subject or bool(SUBJECT_RE.search(subject))


# --- Разбор ---------------------------------------------------------------


def _get_html(msg: email.message.Message) -> str | None:
    html = None
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_content()
    return html


def _get_date(msg: email.message.Message) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            log.warning("не разобрал дату письма: %r", raw)
    return datetime.now(timezone.utc)


def _extract_reviews(soup: BeautifulSoup) -> list[Review]:
    """
    Карточка отзыва в вёрстке письма — это блок, в котором есть звёзды
    и ровно одна ссылка «Ответить». Филиал берём из ближайшего
    предшествующего блока с названием и адресом.

    Такой обход работает и когда в письме один отзыв, и когда их несколько
    (Яндекс шлёт и по одному, и пачками), и когда филиалов в письме больше одного.
    """
    branches = _index_branches(soup)
    reviews: list[Review] = []

    for anchor in soup.find_all("a"):
        if anchor.get_text(strip=True) != REPLY_LABEL:
            continue

        block = _review_block(anchor)
        if block is None:
            log.warning("нашёл «Ответить», но не нашёл карточку отзыва")
            continue

        review = Review()
        review.reply_url = anchor.get("href") or None

        rating, confident, warn = _extract_rating(block)
        review.rating = rating
        review.rating_confident = confident
        if warn:
            review.warnings.append(warn)

        text, truncated = _extract_text(block)
        review.text = text
        review.text_truncated = truncated
        if truncated:
            review.warnings.append(
                f"текст обрезан Яндексом на {TEXT_LIMIT} символах — нужен добор"
            )
        if not text:
            review.warnings.append("пустой текст отзыва")

        name, address, branch_id = _branch_for(block, branches)
        review.branch_name = name
        review.branch_address = address
        review.branch_id = branch_id
        if not address:
            review.warnings.append("не определён филиал")

        reviews.append(review)

    return reviews


def _review_block(anchor: Tag) -> Tag | None:
    """Поднимаемся от ссылки «Ответить» до блока, где есть звёзды."""
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            return None
        if _stars(node):
            return node
    return None


def _stars(node: Tag) -> list[Tag]:
    return [
        img
        for img in node.find_all("img")
        if img.get("width") == STAR_IMG_WIDTH
    ]


def _extract_rating(block: Tag) -> tuple[int | None, bool, str | None]:
    """
    Оценка = количество картинок-звёзд.

    В письмах с оценкой 5 приходят 5 одинаковых картинок. Как выглядит
    оценка ниже — пока не проверено (не было такого письма). Возможны варианты:
      1) звёзд ровно столько, сколько оценка → считаем правильно;
      2) звёзд всегда 5, различаются src → ловим по разным src;
      3) рейтинг не передаётся вовсе → вернём None.
    Поэтому: если все звёзды одинаковые и их 5 — уверены. Если src разные,
    считаем только заполненные (самый частый src). Иначе просим проверить руками.
    """
    stars = _stars(block)
    if not stars:
        return None, False, "звёзды не найдены — оценка неизвестна"

    srcs = [img.get("src", "") for img in stars]
    unique = set(srcs)

    if len(unique) == 1:
        return len(stars), True, None

    # Разные картинки: заполненные — те, что встречаются в начале ряда.
    filled_src = srcs[0]
    filled = sum(1 for s in srcs if s == filled_src)
    return (
        filled,
        False,
        "в ряду звёзд разные картинки — оценка вычислена, проверьте вручную",
    )


def _extract_text(block: Tag) -> tuple[str, bool]:
    """Текст отзыва = содержимое блока без служебных надписей."""
    parts: list[str] = []
    for node in block.stripped_strings:
        s = str(node).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not s or s == REPLY_LABEL:
            continue
        # внутри одной строки схлопываем лишние пробелы, переносы сохраняем
        s = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in s.split("\n"))
        parts.append(s)

    text = "\n".join(parts).strip()
    truncated = text.endswith("...") and len(text.replace("\n", "")) >= TEXT_LIMIT
    return text, truncated


def _index_branches(soup: BeautifulSoup) -> list[tuple[int, str | None, str, str | None]]:
    """
    Собираем филиалы в порядке появления в письме.
    Адрес в вёрстке — ссылка без href, название — ссылка прямо перед ней.
    Возвращаем (позиция в документе, название, адрес, id филиала).
    """
    out = []
    all_nodes = list(soup.descendants)
    positions = {id(n): i for i, n in enumerate(all_nodes)}

    links = soup.find_all("a")
    for i, a in enumerate(links):
        text = a.get_text(" ", strip=True)
        if not text or a.get("href"):
            continue
        if not ADDRESS_RE.match(text):
            continue

        name, branch_id = None, None
        if i > 0:
            prev = links[i - 1]
            prev_text = prev.get_text(" ", strip=True)
            if prev_text and prev_text != REPLY_LABEL:
                name = prev_text
                branch_id = _branch_id_from_url(prev.get("href", ""))

        out.append((positions.get(id(a), 0), name, text, branch_id))

    return out


def _branch_for(
    block: Tag, branches: list[tuple[int, str | None, str, str | None]]
) -> tuple[str | None, str | None, str | None]:
    """Отзыв относится к последнему филиалу, объявленному выше него в письме."""
    if not branches:
        return None, None, None
    if len(branches) == 1:
        _, name, address, bid = branches[0]
        return name, address, bid

    soup = block.find_parent("html") or block
    all_nodes = list(soup.descendants)
    positions = {id(n): i for i, n in enumerate(all_nodes)}
    block_pos = positions.get(id(block), 0)

    chosen = branches[0]
    for entry in branches:
        if entry[0] <= block_pos:
            chosen = entry
        else:
            break

    _, name, address, bid = chosen
    return name, address, bid


def _branch_id_from_url(url: str) -> str | None:
    """
    ID филиала лежит в ссылке кабинета вида /sprav/187474402700/.
    В самом письме ссылки завёрнуты в редирект supersender.yandex.net,
    поэтому здесь ID чаще всего не окажется — достаём, когда он виден.
    Переход по редиректу ради ID делается отдельно (см. README, раздел про ID).
    """
    if not url:
        return None
    m = BRANCH_ID_RE.search(url)
    return m.group(1) if m else None
