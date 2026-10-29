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

    status             TEXT NOT NULL DEFAULT 'new',  -- new | attention | in_progress | answered | skipped
    is_negative        BOOLEAN NOT NULL DEFAULT FALSE,  -- оценка не выше порога (п. 7 ТЗ)
    reply_text         TEXT,                  -- итоговый ответ, ушедший на площадку (п. 9, 11 ТЗ)
    assignee_id        BIGINT,                -- Telegram ID того, кто взял отзыв (п. 8 ТЗ)
    assignee_name      TEXT,
    taken_at           TIMESTAMPTZ,
    email_id           INTEGER REFERENCES emails(id),
    warnings           TEXT[],
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Черновики ответов от ИИ (п. 9 ТЗ). Храним все варианты: по ним видно,
-- как формулировал бот, и с ними сверяется, чтобы ответы не начинались одинаково.
CREATE TABLE IF NOT EXISTS drafts (
    id          SERIAL PRIMARY KEY,
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    style       TEXT NOT NULL,           -- base | another | shorter | warmer | formal | manual
    user_id     BIGINT,
    user_name   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Все копии карточки в Telegram. Негатив уходит в два чата, карточку
-- могут прислать ещё раз через /new — при смене статуса обновляются все
-- копии, иначе в соседнем чате отзыв так и висел бы «свободным».
CREATE TABLE IF NOT EXISTS review_messages (
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    chat_id     TEXT NOT NULL,
    message_id  BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,           -- например last_uid, uidvalidity
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
        # «48/1» — дробь к номеру дома: 2ГИС пишет «Ногина переулок, 48/1»,
        # Яндекс — «пер. Ногина, 48». Здание одно, сравниваем по основному номеру.
        # Литеру («9Б») не трогаем: это может быть другой дом.
        m = re.fullmatch(r"(\d+[а-я]?)/\d+[а-я]?", t)
        if m:
            t = m.group(1)
        tokens.add(t)
    return tokens


# --- Подключение ------------------------------------------------------------


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    return conn


# Миграции: CREATE TABLE IF NOT EXISTS пропускает уже существующую таблицу
# целиком, поэтому новые колонки в неё надо дописывать отдельно. Каждая
# команда безопасна при повторном запуске, порядок значения не имеет.
#
# Добавляя колонку в SCHEMA, ОБЯЗАТЕЛЬНО продублируй её здесь — иначе на
# боевой базе, где таблица уже создана, колонки не появится.
MIGRATIONS = [
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS author TEXT",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS text_full TEXT",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS published_at DATE",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS is_negative BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS reply_text TEXT",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS assignee_id BIGINT",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS assignee_name TEXT",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS taken_at TIMESTAMPTZ",
    "ALTER TABLE branches ADD COLUMN IF NOT EXISTS chat_id TEXT",
    "ALTER TABLE branches ADD COLUMN IF NOT EXISTS external_id TEXT",
    "CREATE INDEX IF NOT EXISTS review_messages_review_idx ON review_messages (review_id)",
    "CREATE INDEX IF NOT EXISTS drafts_review_idx ON drafts (review_id, id)",
    # индексы после колонок: на старой базе колонки может ещё не быть
    "CREATE INDEX IF NOT EXISTS reviews_negative_idx ON reviews (is_negative) WHERE is_negative",
]


def init_schema(conn: psycopg.Connection) -> None:
    """Создаёт недостающие таблицы и дописывает недостающие колонки."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()

    applied = 0
    for sql in MIGRATIONS:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            applied += 1
        except psycopg.Error as e:
            # Одна неудачная миграция не должна валить запуск целиком:
            # остальные применятся, а в логе будет видно, что пошло не так.
            conn.rollback()
            log.error("миграция не применилась: %s — %s", sql[:60], e)

    log.info("схема базы готова (миграций применено: %s)", applied)


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


def rematch_unassigned(conn: psycopg.Connection) -> int:
    """
    Отзывы с неопознанным филиалом пробуем сопоставить заново — после
    правки справочника или правил сравнения адресов. Возвращает, сколько
    отзывов нашли свой филиал.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source, branch_address_raw FROM reviews "
            "WHERE branch_id IS NULL AND branch_address_raw IS NOT NULL"
        )
        rows = cur.fetchall()

    fixed = 0
    for row in rows:
        branch = find_branch(conn, row["source"], row["branch_address_raw"])
        if branch is None:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reviews SET branch_id = %s, "
                "warnings = array_remove(warnings, 'филиал не найден в справочнике') "
                "WHERE id = %s",
                (branch.id, row["id"]),
            )
        fixed += 1
        log.info("отзыв #%s: адрес %r теперь узнан — %s",
                 row["id"], row["branch_address_raw"], branch.address)
    conn.commit()
    return fixed


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
    is_negative: bool = False


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

        # п. 7 ТЗ: отзывы с низкой оценкой сразу получают статус
        # «Требует внимания», чтобы не потеряться в общей ленте.
        negative = is_negative(review.rating, negative_threshold(conn))

        cur.execute(
            """
            INSERT INTO reviews (
                source, branch_id, branch_address_raw,
                rating, rating_confident, text, text_truncated, author,
                reply_url, received_at,
                dedup_key, possible_duplicate, duplicate_of,
                email_id, warnings, is_negative, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                getattr(review, "author", None),
                review.reply_url,
                review.received_at,
                review.dedup_key,
                duplicate_of is not None,
                duplicate_of,
                email_id,
                warnings,
                negative,
                "attention" if negative else "new",
            ),
        )
        review_id = cur.fetchone()["id"]

    conn.commit()
    return SaveResult(
        review_id=review_id,
        possible_duplicate=duplicate_of is not None,
        duplicate_of=duplicate_of,
        branch_id=branch.id if branch else None,
        is_negative=negative,
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
                   r.received_at, r.reply_url, r.status, r.is_negative,
                   r.assignee_name AS assignee,
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
    if status not in ("new", "attention", "in_progress", "answered", "skipped"):
        raise ValueError(f"неизвестный статус: {status}")
    with conn.cursor() as cur:
        cur.execute("UPDATE reviews SET status = %s WHERE id = %s", (status, review_id))
    conn.commit()


# --- Ответственные (п. 8 ТЗ) -------------------------------------------------


OPEN_STATUSES = ("new", "attention")


def claim_review(conn: psycopg.Connection, review_id: int,
                 user_id: int, user_name: str) -> tuple[bool, str | None]:
    """
    Взять отзыв в работу. Возвращает (удалось, кто_ведёт_отзыв).

    Проверка и запись — одним UPDATE: если двое нажмут «Взять в работу»
    одновременно, отзыв достанется ровно одному, второй получит имя первого.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reviews
               SET status = 'in_progress', assignee_id = %s,
                   assignee_name = %s, taken_at = now()
             WHERE id = %s AND status IN ('new', 'attention')
         RETURNING id
            """,
            (user_id, user_name, review_id),
        )
        won = cur.fetchone() is not None
        if not won:
            cur.execute("SELECT assignee_name FROM reviews WHERE id = %s", (review_id,))
            row = cur.fetchone()
            current = row["assignee_name"] if row else None
    conn.commit()
    return (True, user_name) if won else (False, current)


def release_review(conn: psycopg.Connection, review_id: int) -> None:
    """
    Вернуть отзыв в очередь: снять ответственного и вернуть исходный статус.
    Негатив возвращается в «Требует внимания», остальное — в «Новый».
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reviews
               SET status = CASE WHEN is_negative THEN 'attention' ELSE 'new' END,
                   assignee_id = NULL, assignee_name = NULL, taken_at = NULL
             WHERE id = %s
            """,
            (review_id,),
        )
    conn.commit()


def close_review(conn: psycopg.Connection, review_id: int, status: str,
                 user_id: int, user_name: str) -> None:
    """
    Закрыть отзыв («Обработан» или «Не требует ответа»). Если его никто не
    брал в работу, ответственным записывается тот, кто закрыл: в таблице и
    в карточке должно быть видно, кто принял решение.
    """
    if status not in ("answered", "skipped"):
        raise ValueError(f"закрыть можно только как answered/skipped, не {status}")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reviews
               SET status = %s,
                   assignee_id = COALESCE(assignee_id, %s),
                   assignee_name = COALESCE(assignee_name, %s)
             WHERE id = %s
            """,
            (status, user_id, user_name, review_id),
        )
    conn.commit()


def remember_message(conn: psycopg.Connection, review_id: int,
                     chat_id, message_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_messages (review_id, chat_id, message_id) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (review_id, str(chat_id), message_id),
        )
    conn.commit()


def review_messages(conn: psycopg.Connection, review_id: int) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chat_id, message_id FROM review_messages WHERE review_id = %s",
            (review_id,),
        )
        return [(r["chat_id"], r["message_id"]) for r in cur.fetchall()]


# --- Черновики ответов (п. 9 ТЗ) --------------------------------------------


def review_for_draft(conn: psycopg.Connection, review_id: int) -> dict | None:
    """Всё, что нужно модели: текст (полный, если добран), оценка, автор, филиал."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id AS review_id, r.source, r.rating,
                   COALESCE(r.text_full, r.text) AS text,
                   (r.text_truncated AND r.text_full IS NULL) AS text_truncated,
                   r.author, r.is_negative,
                   COALESCE(b.address, r.branch_address_raw) AS branch_address
            FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id
            WHERE r.id = %s
            """,
            (review_id,),
        )
        return cur.fetchone()


def save_draft(conn: psycopg.Connection, review_id: int, text: str, style: str,
               user_id: int | None = None, user_name: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO drafts (review_id, text, style, user_id, user_name) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (review_id, text, style, user_id, user_name),
        )
        draft_id = cur.fetchone()["id"]
    conn.commit()
    return draft_id


def latest_draft(conn: psycopg.Connection, review_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, text, style FROM drafts WHERE review_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (review_id,),
        )
        return cur.fetchone()


def recent_draft_texts(conn: psycopg.Connection, limit: int = 15,
                       exclude_review: int | None = None) -> list[str]:
    """
    Последние ответы по ДРУГИМ отзывам — чтобы новый не начинался так же.
    Сначала опубликованные ответы, потом свежие черновики.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT text FROM (
                SELECT reply_text AS text, id AS rid, 0 AS prio FROM reviews
                 WHERE reply_text IS NOT NULL AND id IS DISTINCT FROM %s
                UNION ALL
                SELECT text, review_id AS rid, 1 AS prio FROM drafts
                 WHERE review_id IS DISTINCT FROM %s
            ) t
            ORDER BY prio, rid DESC
            LIMIT %s
            """,
            (exclude_review, exclude_review, limit),
        )
        return [r["text"] for r in cur.fetchall()]


def set_reply(conn: psycopg.Connection, review_id: int, text: str) -> None:
    """Итоговый ответ — то, что ушло на площадку. Выгружается в таблицу."""
    with conn.cursor() as cur:
        cur.execute("UPDATE reviews SET reply_text = %s WHERE id = %s", (text, review_id))
    conn.commit()


# --- Негативные отзывы (пп. 7 и 17 ТЗ) -------------------------------------

NEGATIVE_THRESHOLD_KEY = "negative_threshold"
DEFAULT_NEGATIVE_THRESHOLD = 3  # отзывы с оценкой 1-3 требуют внимания


def negative_threshold(conn: psycopg.Connection) -> int:
    """
    Порог негатива: оценка не выше этого числа считается проблемной.
    Настраивается администратором (п. 17 ТЗ), по умолчанию 3.
    """
    raw = get_state(conn, NEGATIVE_THRESHOLD_KEY)
    try:
        value = int(raw) if raw else DEFAULT_NEGATIVE_THRESHOLD
    except ValueError:
        log.warning("испорчен порог негатива %r, беру %s", raw, DEFAULT_NEGATIVE_THRESHOLD)
        return DEFAULT_NEGATIVE_THRESHOLD
    return value if 1 <= value <= 5 else DEFAULT_NEGATIVE_THRESHOLD


def set_negative_threshold(conn: psycopg.Connection, value: int) -> None:
    if not 1 <= value <= 5:
        raise ValueError("порог негатива должен быть от 1 до 5")
    set_state(conn, NEGATIVE_THRESHOLD_KEY, value)


def is_negative(rating: int | None, threshold: int) -> bool:
    """
    Отзыв без оценки негативным не считается: лучше не поднимать ложную
    тревогу, тем более что в карточке уже стоит пометка о неизвестной оценке.
    """
    return rating is not None and rating <= threshold


# --- Служебное состояние ----------------------------------------------------


def get_state(conn: psycopg.Connection, key: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def set_state(conn: psycopg.Connection, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, str(value)),
        )
    conn.commit()


def last_letter_at(conn: psycopg.Connection, source: str) -> datetime | None:
    """Когда по площадке в последний раз приходило письмо — для контроля тишины."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(received_at) AS t FROM emails WHERE source = %s", (source,)
        )
        row = cur.fetchone()
        return row["t"] if row else None
