"""
Резервная копия базы (п. 21 ТЗ).

Копия — zip-архив: по JSON-файлу на таблицу. Делается средствами Python,
без pg_dump: на сервере Railway его может не быть, а так копия работает
везде и восстанавливается скриптом restore_backup.py в любую PostgreSQL.

Бот присылает копию администратору в Telegram раз в неделю и по кнопке.
Это дополнительно к резервным копиям самого Railway (вкладка Backups у
сервиса PostgreSQL), а не вместо них: копия в Telegram лежит отдельно от
сервера и переживёт даже удаление проекта.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import date, datetime

FORMAT_VERSION = 1

# Порядок важен для восстановления: сначала те, на кого ссылаются
TABLES = ["branches", "emails", "reviews", "drafts", "review_messages",
          "review_topics", "review_comments", "state"]

# Таблицы с автонумерацией id — после восстановления двигаем счётчик
SERIAL_TABLES = ["branches", "emails", "reviews", "drafts", "review_comments"]


def _encode(value):
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(value)).decode()}
    return value


def _decode(value):
    if isinstance(value, dict):
        if "$dt" in value:
            return datetime.fromisoformat(value["$dt"])
        if "$date" in value:
            return date.fromisoformat(value["$date"])
        if "$b64" in value:
            return base64.b64decode(value["$b64"])
    return value


def _existing(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        return {r["tablename"] for r in cur.fetchall()}


def make_backup(conn, with_raw_emails: bool = True) -> tuple[bytes, dict[str, int]]:
    """Архив и сколько строк в каждой таблице. Читаем в одной транзакции — копия целостная."""
    counts: dict[str, int] = {}
    buf = io.BytesIO()
    present = _existing(conn)
    conn.commit()  # «SET TRANSACTION» должен быть первым в транзакции
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for table in TABLES:
                if table not in present:
                    continue
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {table}")  # noqa: S608 — имя из списка выше
                    rows = cur.fetchall()
                if table == "emails" and not with_raw_emails:
                    rows = [{**r, "raw": None} for r in rows]
                counts[table] = len(rows)
                z.writestr(f"{table}.json", json.dumps(
                    [{k: _encode(v) for k, v in r.items()} for r in rows],
                    ensure_ascii=False))
            z.writestr("meta.json", json.dumps({
                "format": FORMAT_VERSION, "created_at": datetime.now().astimezone().isoformat(),
                "counts": counts}, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return buf.getvalue(), counts


def read_backup(data: bytes) -> tuple[dict, dict[str, list[dict]]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        meta = json.loads(z.read("meta.json"))
        tables = {}
        for table in TABLES:
            name = f"{table}.json"
            if name in z.namelist():
                tables[table] = [{k: _decode(v) for k, v in r.items()}
                                 for r in json.loads(z.read(name))]
    return meta, tables


def restore(conn, data: bytes) -> dict[str, int]:
    """
    Залить копию в базу. Существующие данные в этих таблицах УДАЛЯЮТСЯ —
    поэтому восстанавливать только в новую или заведомо испорченную базу.
    """
    import storage

    meta, tables = read_backup(data)
    if meta.get("format") != FORMAT_VERSION:
        raise ValueError(f"неизвестный формат копии: {meta.get('format')}")
    storage.init_schema(conn)

    restored = {}
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("TRUNCATE " + ", ".join(reversed(TABLES)) + " RESTART IDENTITY CASCADE")
            for table in TABLES:
                rows = tables.get(table, [])
                for row in rows:
                    cols = list(row)
                    cur.execute(
                        f"INSERT INTO {table} ({', '.join(cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})",
                        [row[c] for c in cols],
                    )
                restored[table] = len(rows)
            for table in SERIAL_TABLES:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)"
                )
    return restored


def describe(counts: dict[str, int]) -> str:
    return (f"отзывов {counts.get('reviews', 0)}, писем {counts.get('emails', 0)}, "
            f"филиалов {counts.get('branches', 0)}, комментариев {counts.get('review_comments', 0)}")
