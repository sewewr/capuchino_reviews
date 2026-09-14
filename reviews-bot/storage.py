"""
Хранилище: схема базы, справочник филиалов, сохранение отзывов с дедупликацией.

PostgreSQL, потому что бот живёт на Railway: файловая система там затирается
при каждом деплое, и SQLite-файл вместе с ней.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    id            SERIAL PRIMARY KEY,
    source        TEXT NOT NULL,              -- yandex | 2gis | tripadvisor
    external_id   TEXT,                       -- ID филиала на площадке, если известен
    name          TEXT NOT NULL,
    address       TEXT NOT NULL,              -- как написано в карточке площадки
    address_norm  TEXT NOT NULL,              -- нормализованный адрес для сопоставления
    chat_id       TEXT,                       -- чат Telegram для этого филиала
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, address_norm)
);

CREATE TABLE IF NOT EXISTS emails (
    id            SERIAL PRIMARY KEY,
    message_id    TEXT UNIQUE,                -- защита от повторной обработки письма
    source        TEXT,
    received_at   TIMESTAMPTZ,
    parsed_ok     BOOLEAN NOT NULL DEFAULT FALSE,
    error         TEXT,
    raw           BYTEA,                      -- храним оригинал: если разбор упал, вернёмся
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reviews (
    id                 SERIAL PRIMARY KEY,
    source             TEXT NOT NULL,
    branch_id          INTEGER REFERENCES branches(id),
    branch_address_raw TEXT,                  -- адрес из письма, если филиал не опознан

    rating             SMALLINT,              -- NULL = не определили
    rating_confident   BOOLEAN NOT NULL DEFAULT FALSE,
    text               TEXT NOT NULL DEFAULT '',
    text_truncated     BOOLEAN NOT NULL DEFAULT FALSE,
    text_full          TEXT,                  -- добирается вручную при ответе
    author             TEXT,                  -- в письме его нет, добирается вручную
    published_at       DATE,                  -- дата публикации, добирается вручную

    reply_url          TEXT,
    received_at        TIMESTAMPTZ NOT NULL,  -- дата письма, отстаёт от публикации на 1-2 дня

    dedup_key          TEXT NOT NULL,
    possible_duplicate BOOLEAN NOT NULL DEFAULT FALSE,
    duplicate_of       INTEGER REFERENCES reviews(id),

    status             TEXT NOT NULL DEFAULT 'new',  -- new | in_progress | answered | skipped
    email_id           INTEGER REFERENCES emails(id),
    warnings           TEXT[],
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reviews_dedup_idx  ON reviews (dedup_key);
CREATE INDEX IF NOT EXISTS reviews_status_idx ON reviews (status);
CREATE INDEX IF NOT EXISTS reviews_branch_idx ON reviews (branch_id);
"""


# --- Нормализация адресов ---------------------------------------------------

# Адрес из письма и адрес из справочника пишутся по-разному, причём
# отличается не только написание, но и порядок слов:
#   письмо:     «Волгоград, Рабоче-Крестьянская улица, 9Б»
#   справочник: «Волгоград, ул. Рабоче-Крестьянская, 9Б»
# Поэтому слова-типы («улица», «проспект») выбрасываем совсем, а оставшиеся
# сортируем: сравнение получается независимым от порядка и от сокращений.
_STREET_TYPES = {
    "улица", "ул", "проспект", "просп", "пркт", "пр", "проезд",
    "переулок", "пер", "площадь", "пл", "бульвар", "бр", "бул",
    "шоссе", "ш", "набережная", "наб", "город", "г", "дом", "д",
    "строение", "стр", "литер", "лит", "корпус", "корп", "к",
    "микрорайон", "мкр", "район", "рн", "имени", "им",
}


def normalize_address(address: str) -> str:
    """
    Приводим адрес к виду, по которому его можно сопоставить со справочником:
    нижний регистр, без пунктуации, без слов-типов улиц, слова отсортированы.
    """
    return " ".join(sorted(address_tokens(address)))


def address_tokens(address: str) -> set[str]:
    """
    Значимые слова адреса: город, название улицы, номер дома.

    Инициалы выбрасываем: «просп. имени В.И. Ленина» и «проспект Ленина» —
    один и тот же адрес, а из «В.И.» получились бы мусорные слова «в» и «и».
    """
    s = (address or "").lower().replace("ё", "е").replace("\u00a0", " ")
    s = re.sub(r"[.,;]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    tokens = set()
    for t in s.split():
        if not t or t in _STREET_TYPES:
            continue
        if len(t) == 1 and t.isalpha():  # инициал
            continue
        tokens.add(t)
    return tokens


# --- Подключение ------------------------------------------------------------


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    return conn


def init_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()
    log.info("схема базы готова")


# --- Справочник филиалов ----------------------------------------------------


@dataclass
class Branch:
    id: int
    source: str
    name: str
    address: str
    chat_id: str | None
    external_id: str | None


def upsert_branch(
    conn: psycopg.Connection,
    source: str,
    name: str,
    address: str,
    chat_id: str | None = None,
    external_id: str | None = None,
) -> int:
    """Добавить или обновить филиал. Адреса берём ровно как в карточке площадки."""
    norm = normalize_address(address)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO branches (source, external_id, name, address, address_norm, chat_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, address_norm) DO UPDATE
                SET name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    chat_id = COALESCE(EXCLUDED.chat_id, branches.chat_id),
                    external_id = COALESCE(EXCLUDED.external_id, branches.external_id)
            RETURNING id
            """,
            (source, external_id, name, address, norm, chat_id),
        )
        return cur.fetchone()["id"]


def find_branch(
    conn: psycopg.Connection,
    source: str,
    address: str | None,
    external_id: str | None = None,
) -> Branch | None:
    """
    Ищем филиал: сначала по ID площадки (надёжнее), потом по нормализованному адресу.
    """
    with conn.cursor() as cur:
        if external_id:
            cur.execute(
                "SELECT id, source, name, address, chat_id, external_id "
                "FROM branches WHERE source = %s AND external_id = %s",
                (source, external_id),
            )
            row = cur.fetchone()
            if row:
                return Branch(**row)

        if not address:
            return None

        norm = normalize_address(address)
        cur.execute(
            "SELECT id, source, name, address, chat_id, external_id "
            "FROM branches WHERE source = %s AND address_norm = %s",
            (source, norm),
        )
        row = cur.fetchone()
        if row:
            return Branch(**row)

        # Запасной вариант: в одном из адресов может быть лишнее слово
        # (корпус, индекс, «стр. 1»). Филиалов десяток, поэтому спокойно
        # сравниваем наборы слов на вложенность.
        probe = address_tokens(address)
        if not probe:
            return None

        cur.execute(
            "SELECT id, source, name, address, chat_id, external_id "
            "FROM branches WHERE source = %s AND active",
            (source,),
        )
        candidates = []
        for row in cur.fetchall():
            tokens = address_tokens(row["address"])
            if tokens <= probe or probe <= tokens:
                candidates.append((len(tokens & probe), row))

        if not candidates:
            return None
        if len(candidates) > 1:
            log.warning(
                "адрес %r подошёл к нескольким филиалам — беру самый близкий", address
            )
        candidates.sort(key=lambda x: x[0], reverse=True)
        return Branch(**candidates[0][1])


# --- Письма -----------------------------------------------------------------


def email_already_processed(conn: psycopg.Connection, message_id: str | None) -> bool:
    if not message_id:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM emails WHERE message_id = %s", (message_id,))
        return cur.fetchone() is not None


def save_email(
    conn: psycopg.Connection,
    message_id: str | None,
    source: str | None,
    received_at: datetime | None,
    raw: bytes,
    parsed_ok: bool,
    error: str | None = None,
) -> int:
    """
    Сохраняем оригинал письма. Если разбор упал, письмо не теряется:
    его можно разобрать повторно после правки парсера.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO emails (message_id, source, received_at, raw, parsed_ok, error)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id) DO UPDATE
                SET parsed_ok = EXCLUDED.parsed_ok, error = EXCLUDED.error
            RETURNING id
            """,
            (message_id, source, received_at, raw, parsed_ok, error),
        )
        return cur.fetchone()["id"]


# --- Отзывы -----------------------------------------------------------------


@dataclass
class SaveResult:
    review_id: int
    possible_duplicate: bool
    duplicate_of: int | None
    branch_id: int | None


def save_review(conn: psycopg.Connection, review, email_id: int | None = None) -> SaveResult:
    """
    Сохранить отзыв.

    ID отзыва площадка не даёт, поэтому совпадение ключа НЕ означает дубль
    наверняка: два гостя могут написать одинаково коротко. Поэтому отзыв
    сохраняется всегда, а при совпадении помечается «возможный дубль»
    и ссылается на первый такой отзыв. Решение — за сотрудником.
    """
    branch = find_branch(conn, review.source, review.branch_address, review.branch_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM reviews WHERE dedup_key = %s "
            "ORDER BY id LIMIT 1",
            (review.dedup_key,),
        )
        existing = cur.fetchone()
        duplicate_of = existing["id"] if existing else None

        warnings = list(review.warnings)
        if branch is None:
            warnings.append("филиал не найден в справочнике")

        cur.execute(
            """
            INSERT INTO reviews (
                source, branch_id, branch_address_raw,
                rating, rating_confident, text, text_truncated,
                reply_url, received_at,
                dedup_key, possible_duplicate, duplicate_of,
                email_id, warnings
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                review.source,
                branch.id if branch else None,
                review.branch_address,
                review.rating,
                review.rating_confident,
                review.text,
                review.text_truncated,
                review.reply_url,
                review.received_at,
                review.dedup_key,
                duplicate_of is not None,
                duplicate_of,
                email_id,
                warnings,
            ),
        )
        review_id = cur.fetchone()["id"]

    conn.commit()
    return SaveResult(
        review_id=review_id,
        possible_duplicate=duplicate_of is not None,
        duplicate_of=duplicate_of,
        branch_id=branch.id if branch else None,
    )


def add_full_text(
    conn: psycopg.Connection,
    review_id: int,
    text_full: str,
    author: str | None = None,
    published_at: str | None = None,
) -> None:
    """Добор данных, которых нет в письме: полный текст, автор, дата публикации."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE reviews SET text_full = %s, "
            "author = COALESCE(%s, author), "
            "published_at = COALESCE(%s::date, published_at) "
            "WHERE id = %s",
            (text_full, author, published_at, review_id),
        )
    conn.commit()


def get_card_data(conn: psycopg.Connection, review_id: int) -> dict | None:
    """Всё, что нужно для карточки отзыва в Telegram."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id AS review_id, r.source, r.rating, r.rating_confident,
                   r.text, r.text_full, r.text_truncated, r.author, r.published_at,
                   r.received_at, r.reply_url, r.status,
                   r.possible_duplicate, r.duplicate_of,
                   b.name AS branch_name, b.address AS branch_address
            FROM reviews r
            LEFT JOIN branches b ON b.id = r.branch_id
            WHERE r.id = %s
            """,
            (review_id,),
        )
        return cur.fetchone()


def set_status(conn: psycopg.Connection, review_id: int, status: str) -> None:
    if status not in ("new", "in_progress", "answered", "skipped"):
        raise ValueError(f"неизвестный статус: {status}")
    with conn.cursor() as cur:
        cur.execute("UPDATE reviews SET status = %s WHERE id = %s", (status, review_id))
    conn.commit()
