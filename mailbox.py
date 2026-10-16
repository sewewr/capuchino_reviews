"""
Чтение почтового ящика по IMAP.

Принципы:
  — только чтение: письма не помечаются прочитанными и не перемещаются,
    владелец ящика не заметит работы бота (используется BODY.PEEK);
  — повторная обработка отсекается по Message-ID в базе, а не по флагам;
  — чтобы не перебирать архив каждый раз, помним последний UID (водяной знак);
  — письма старше отсечки не берём: в ящике может лежать годовой архив,
    и при первом запуске бот не должен вывалить его в Telegram разом.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from email import policy

log = logging.getLogger(__name__)

IMAP_PORT = 993

# Mail.ru отдаёт большие письма целиком; с запасом, но не безлимитно
imaplib._MAXLINE = 10_000_000  # noqa: SLF001


@dataclass
class Letter:
    uid: int
    message_id: str | None
    sender: str
    subject: str
    raw: bytes


class MailboxError(Exception):
    """Не удалось прочитать ящик. Не роняет бота — попробуем в следующий раз."""


class Mailbox:
    """Подключение к ящику. Используется как контекстный менеджер."""

    def __init__(self, host: str, user: str, password: str, folder: str = "INBOX"):
        self.host = host
        self.user = user
        self.password = password
        self.folder = folder
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "Mailbox":
        try:
            self._conn = imaplib.IMAP4_SSL(self.host, IMAP_PORT)
            self._conn.login(self.user, self.password)
            # readonly: ящик не меняем вообще
            self._conn.select(self.folder, readonly=True)
        except imaplib.IMAP4.error as e:
            raise MailboxError(f"не вошёл в ящик {self.user}: {e}") from e
        except OSError as e:
            raise MailboxError(f"не достучался до {self.host}: {e}") from e
        return self

    def __exit__(self, *exc) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
            self._conn.logout()
        except Exception:  # разрыв при закрытии не важен
            pass
        self._conn = None

    # --- внутреннее ---

    def _cmd(self, *args) -> list:
        status, data = self._conn.uid(*args)
        if status != "OK":
            raise MailboxError(f"IMAP ответил {status} на {args[0]}")
        return data

    def uid_validity(self) -> str:
        """
        Если сервер пересоздал ящик, UID'ы перестают что-либо значить.
        Сравнивая это число, понимаем, что водяной знак пора сбросить.
        """
        status, data = self._conn.status(self.folder, "(UIDVALIDITY)")
        if status != "OK" or not data:
            return ""
        m = re.search(rb"UIDVALIDITY (\d+)", data[0])
        return m.group(1).decode() if m else ""

    # --- поиск и загрузка ---

    def search(self, senders: list[str], since: date, min_uid: int = 0) -> list[int]:
        """UID'ы писем от указанных отправителей, не старше отсечки."""
        criteria_date = since.strftime("%d-%b-%Y")
        found: set[int] = set()

        for sender in senders:
            query = ["SEARCH", None, "SINCE", criteria_date, "FROM", f'"{sender}"']
            if min_uid:
                query += ["UID", f"{min_uid + 1}:*"]
            data = self._cmd(*query)
            if data and data[0]:
                found.update(int(x) for x in data[0].split())

        return sorted(found)

    def message_ids(self, uids: list[int]) -> dict[int, str | None]:
        """
        Сначала забираем только заголовки — это дёшево. По Message-ID
        отсеиваем уже обработанные и не тянем их тела повторно.
        """
        if not uids:
            return {}

        out: dict[int, str | None] = {}
        for chunk in _chunks(uids, 200):
            data = self._cmd(
                "FETCH",
                ",".join(str(u) for u in chunk),
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            for uid, raw in _pairs(data):
                m = re.search(rb"Message-I[Dd]:\s*(<[^>]+>)", raw)
                out[uid] = m.group(1).decode(errors="replace") if m else None
        return out

    def fetch(self, uid: int) -> Letter:
        """Письмо целиком. PEEK — чтобы не помечать прочитанным."""
        data = self._cmd("FETCH", str(uid), "(BODY.PEEK[])")
        for got_uid, raw in _pairs(data):
            if got_uid != uid:
                continue
            msg = email.message_from_bytes(raw, policy=policy.default)
            return Letter(
                uid=uid,
                message_id=str(msg.get("Message-ID", "")) or None,
                sender=str(msg.get("From", "")),
                subject=str(msg.get("Subject", "")),
                raw=raw,
            )
        raise MailboxError(f"письмо uid={uid} не загрузилось")


# --- разбор ответов IMAP ---------------------------------------------------


def _pairs(data: list) -> list[tuple[int, bytes]]:
    """
    FETCH возвращает чередование: описание (с UID) и тело. Собираем пары.
    """
    out = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        header, body = item[0], item[1]
        m = re.search(rb"UID (\d+)", header or b"")
        if m and body:
            out.append((int(m.group(1)), body))
    return out


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_cutoff(raw: str | None, default: date) -> date:
    """Отсечка по дате из переменной окружения: 2026-10-14 или 14.10.2026."""
    if not raw:
        return default
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    log.warning("не разобрал IMAP_SINCE=%r, беру %s", raw, default)
    return default
