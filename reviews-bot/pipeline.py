"""
Конвейер: почта → парсер → база → Telegram.

Один проход (`poll_once`) делает:
  1. ищет в ящике новые письма от площадок, не старше отсечки;
  2. отсеивает уже обработанные по Message-ID;
  3. разбирает подходящим парсером, сохраняя оригинал письма в базу;
  4. сохраняет отзывы с дедупликацией;
  5. отправляет карточки в Telegram.

Любая ошибка на одном письме не роняет остальные: письмо остаётся в базе
с пометкой об ошибке, и его можно разобрать повторно после правки парсера.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import parsers
import storage
from mailbox import Letter, Mailbox, MailboxError

log = logging.getLogger(__name__)

# Отправители, письма которых нас интересуют
SENDERS = ["business.yandex.ru", "account.2gis.com"]

# Сколько ждать писем от площадки, прежде чем заподозрить неладное.
# У Яндекса отзывы приходят пачками и нерегулярно, поэтому срок с запасом.
SILENCE_DAYS = {"yandex": 14, "2gis": 14}

STATE_LAST_UID = "imap_last_uid"
STATE_UIDVALIDITY = "imap_uidvalidity"


@dataclass
class PollResult:
    letters_seen: int = 0
    letters_new: int = 0
    reviews_saved: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"писем новых: {self.letters_new}", f"отзывов: {self.reviews_saved}"]
        if self.duplicates:
            parts.append(f"возможных дублей: {self.duplicates}")
        if self.errors:
            parts.append(f"ошибок: {len(self.errors)}")
        return ", ".join(parts)


def poll_once(
    conn,
    mailbox: Mailbox,
    since: date,
    on_review=None,
) -> PollResult:
    """
    Один проход по ящику. `on_review(review_id, card_chat_hint)` вызывается
    для каждого нового отзыва — через него карточка уходит в Telegram.
    """
    result = PollResult()

    last_uid = _resume_point(conn, mailbox)
    uids = mailbox.search(SENDERS, since=since, min_uid=last_uid)
    result.letters_seen = len(uids)
    if not uids:
        return result

    known = mailbox.message_ids(uids)
    fresh = [
        uid for uid in uids
        if not storage.email_already_processed(conn, known.get(uid))
    ]
    result.letters_new = len(fresh)

    max_uid = last_uid
    for uid in fresh:
        try:
            letter = mailbox.fetch(uid)
            _handle_letter(conn, letter, result, on_review)
        except MailboxError as e:
            # проблема связи — водяной знак не двигаем, вернёмся к письму позже
            result.errors.append(f"uid={uid}: {e}")
            log.warning("не забрал письмо uid=%s: %s", uid, e)
            break
        except Exception as e:  # ошибка разбора конкретного письма
            result.errors.append(f"uid={uid}: {e}")
            log.exception("письмо uid=%s не обработано", uid)
        max_uid = max(max_uid, uid)

    if max_uid > last_uid:
        storage.set_state(conn, STATE_LAST_UID, max_uid)

    return result


def _handle_letter(conn, letter: Letter, result: PollResult, on_review) -> None:
    parser = parsers.pick(letter.sender, letter.subject)
    if parser is None:
        log.debug("письмо не от площадки: %s", letter.sender)
        return

    source = getattr(parser, "SOURCE", None) or _source_of(parser)

    try:
        reviews = parser.parse(letter.raw)
    except parsers.ParseError as e:
        # письмо сохраняем целиком: разберём повторно после правки парсера
        storage.save_email(
            conn, letter.message_id, source, None, letter.raw, False, str(e)
        )
        conn.commit()
        result.errors.append(f"{source}: {e}")
        log.warning("не разобрал письмо от %s: %s", source, e)
        return

    email_id = storage.save_email(
        conn, letter.message_id, source, reviews[0].received_at, letter.raw, True
    )
    conn.commit()

    for review in reviews:
        saved = storage.save_review(conn, review, email_id=email_id)
        result.reviews_saved += 1
        if saved.possible_duplicate:
            result.duplicates += 1
        if on_review:
            on_review(saved.review_id, saved.branch_id)


def _source_of(parser) -> str:
    name = getattr(parser, "__name__", "")
    return "2gis" if "gis" in name else "yandex"


def _resume_point(conn, mailbox: Mailbox) -> int:
    """
    Продолжаем с последнего известного UID. Если сервер пересоздал ящик,
    UID'ы больше ничего не значат — начинаем заново, от дублей защитит
    проверка по Message-ID.
    """
    current = mailbox.uid_validity()
    stored = storage.get_state(conn, STATE_UIDVALIDITY)

    if current and stored and current != stored:
        log.warning("ящик пересоздан (UIDVALIDITY %s → %s), сбрасываю отметку", stored, current)
        storage.set_state(conn, STATE_LAST_UID, 0)

    if current and current != stored:
        storage.set_state(conn, STATE_UIDVALIDITY, current)

    raw = storage.get_state(conn, STATE_LAST_UID)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


# --- Контроль тишины -------------------------------------------------------


def silent_sources(conn, now: datetime | None = None) -> list[tuple[str, int]]:
    """
    Площадки, от которых давно нет писем. Нужен, потому что молчание
    выглядит одинаково и когда отзывов просто нет, и когда бот оторвался
    от почты. Возвращает пары (площадка, дней молчания).
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for source, limit in SILENCE_DAYS.items():
        last = storage.last_letter_at(conn, source)
        if last is None:
            continue  # ещё ни одного письма — отсчитывать не от чего
        days = (now - last).days
        if days >= limit:
            out.append((source, days))
    return out


def default_since(days_back: int = 1) -> date:
    """Отсечка по умолчанию: вчера, чтобы при первом запуске не поднять архив."""
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
