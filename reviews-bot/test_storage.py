"""
Проверка хранилища на живой базе.

Запуск: DATABASE_URL=postgresql://... python test_storage.py
"""

import os
from pathlib import Path

import storage
import email
from email import policy

import parsers

DB = os.environ.get("DATABASE_URL")


def main() -> None:
    conn = storage.connect(DB)

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS review_comments, review_topics, drafts, review_messages, reviews, emails, branches CASCADE")
    conn.commit()
    storage.init_schema(conn)

    print("--- справочник филиалов ---")
    # адрес написан НЕ так, как в письме: проверяем нормализацию
    bid = storage.upsert_branch(
        conn, "yandex", "Капучино",
        "Волгоград, ул. Рабоче-Крестьянская, 9Б",
        chat_id="-1001234567890",
    )
    storage.upsert_branch(
        conn, "yandex", "Капучино", "Волгоград, улица Рокоссовского, 54",
    )
    storage.upsert_branch(
        conn, "2gis", "Капучино",
        "бул. 30-летия Победы, 21, район Семь Ветров, Волгоград",
    )
    conn.commit()
    print(f"  добавлено 3 филиала, первый id={bid}")

    print("\n--- сопоставление адресов ---")
    for probe in [
        "Волгоград, Рабоче-Крестьянская улица, 9Б",   # как в письме
        "Волгоград, ул. Рокоссовского, 54",
        "Волгоград, улица Неизвестная, 1",
    ]:
        found = storage.find_branch(conn, "yandex", probe)
        print(f"  {probe:<48} -> {found.address if found else 'НЕ НАЙДЕН'}")

    print("\n--- разбор и сохранение писем ---")
    for path in sorted(Path("samples").glob("*.eml")):
        raw = path.read_bytes()
        parser = parsers.pick(str(email.message_from_bytes(raw, policy=policy.default).get("From", "")))
        reviews = parser.parse(raw)
        eid = storage.save_email(
            conn, reviews[0].message_id, reviews[0].source, reviews[0].received_at, raw, True
        )
        conn.commit()
        for r in reviews:
            res = storage.save_review(conn, r, email_id=eid)
            dup = f"  ВОЗМОЖНЫЙ ДУБЛЬ (id={res.duplicate_of})" if res.possible_duplicate else ""
            print(f"  {path.name}: отзыв id={res.review_id} филиал={res.branch_id}{dup}")

    print("\n--- повторная обработка тех же писем ---")
    for path in sorted(Path("samples").glob("*.eml")):
        raw = path.read_bytes()
        parser = parsers.pick(str(email.message_from_bytes(raw, policy=policy.default).get("From", "")))
        reviews = parser.parse(raw)
        seen = storage.email_already_processed(conn, reviews[0].message_id)
        print(f"  {path.name}: письмо уже обработано -> {seen}")
        if seen:
            continue
        for r in reviews:
            storage.save_review(conn, r)

    print("\n--- добор полного текста ---")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reviews WHERE text_truncated LIMIT 1")
        row = cur.fetchone()
    if row:
        storage.add_full_text(
            conn, row["id"],
            "Моё самое любимое место... (полный текст, вставленный сотрудником)",
            author="Мария К.", published_at="2026-08-26",
        )
        print(f"  отзыв id={row['id']}: добавлены полный текст, автор и дата публикации")

    print("\n--- что в базе ---")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id, r.source, b.address, r.rating, r.text_truncated, "
            "r.author, r.published_at, r.possible_duplicate, r.status "
            "FROM reviews r LEFT JOIN branches b ON b.id = r.branch_id ORDER BY r.id"
        )
        for row in cur.fetchall():
            print(
                f"  id={row['id']} [{row['source']}] филиал={'да' if row['address'] else 'НЕТ'} оценка={row['rating']} "
                f"обрезан={row['text_truncated']} автор={row['author'] or '—'} "
                f"дата={row['published_at'] or '—'} статус={row['status']}"
            )

    conn.close()


if __name__ == "__main__":
    main()


def test_migration_from_old_schema() -> None:
    """
    Проверка обновления боевой базы: таблицы уже есть, новых колонок нет.

    Этот случай однажды уронил бота на сервере: CREATE TABLE IF NOT EXISTS
    пропускает существующую таблицу целиком, поэтому новые колонки надо
    дописывать миграциями. Тесты на чистой базе такое не ловят.

    Запуск: DATABASE_URL=... python -c "import test_storage; test_storage.test_migration_from_old_schema()"
    """
    import psycopg
    from psycopg.rows import dict_row

    old = """
    DROP TABLE IF EXISTS review_comments, review_topics, drafts, review_messages, reviews, emails, branches, state CASCADE;
    CREATE TABLE branches (
        id SERIAL PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
        address TEXT NOT NULL, address_norm TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source, address_norm));
    CREATE TABLE emails (
        id SERIAL PRIMARY KEY, message_id TEXT UNIQUE, source TEXT,
        received_at TIMESTAMPTZ, parsed_ok BOOLEAN NOT NULL DEFAULT FALSE,
        error TEXT, raw BYTEA, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE reviews (
        id SERIAL PRIMARY KEY, source TEXT NOT NULL,
        branch_id INTEGER REFERENCES branches(id), branch_address_raw TEXT,
        rating SMALLINT, rating_confident BOOLEAN NOT NULL DEFAULT FALSE,
        text TEXT NOT NULL DEFAULT '', text_truncated BOOLEAN NOT NULL DEFAULT FALSE,
        reply_url TEXT, received_at TIMESTAMPTZ NOT NULL, dedup_key TEXT NOT NULL,
        possible_duplicate BOOLEAN NOT NULL DEFAULT FALSE,
        duplicate_of INTEGER REFERENCES reviews(id),
        status TEXT NOT NULL DEFAULT 'new', email_id INTEGER REFERENCES emails(id),
        warnings TEXT[], created_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
    """

    raw = psycopg.connect(DB, row_factory=dict_row)
    with raw.cursor() as cur:
        cur.execute(old)
    raw.commit()
    raw.close()

    conn = storage.connect(DB)
    storage.init_schema(conn)
    storage.init_schema(conn)  # повторный запуск не должен ломаться

    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='reviews'"
        )
        cols = {r["column_name"] for r in cur.fetchall()}

    missing = {"is_negative", "author", "text_full", "published_at",
               "assignee_id", "assignee_name", "taken_at", "reply_text"} - cols
    assert not missing, f"колонки не добавились: {missing}"

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('review_messages') AS t")
        assert cur.fetchone()["t"], "таблица review_messages не создалась"
        cur.execute("SELECT to_regclass('drafts') AS t")
        assert cur.fetchone()["t"], "таблица drafts не создалась"
    print("  миграция со старой базы: ок")
    conn.close()


def test_rematch() -> None:
    """Отзыв с неузнанным адресом находит филиал после правки правил сравнения."""
    from datetime import datetime, timezone

    conn = storage.connect(DB)
    storage.init_schema(conn)
    from seed_branches import BRANCHES, SOURCES
    for source in SOURCES:
        for b in BRANCHES:
            storage.upsert_branch(conn, source, b["name"], b["address"])
    with conn.cursor() as cur:
        # так отзыв лёг в базу до правки: филиала нет, есть адрес из письма
        cur.execute(
            "INSERT INTO reviews (source, branch_id, branch_address_raw, rating, text, "
            "received_at, dedup_key, warnings) VALUES ('2gis', NULL, %s, 5, 'Вкусно', %s, "
            "'rematch-test', ARRAY['филиал не найден в справочнике']) RETURNING id",
            ("Ногина переулок, 48/1", datetime.now(timezone.utc)),
        )
        rid = cur.fetchone()["id"]
    conn.commit()

    fixed = storage.rematch_unassigned(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT b.address, r.warnings FROM reviews r "
                    "JOIN branches b ON b.id = r.branch_id WHERE r.id = %s", (rid,))
        row = cur.fetchone()
    print("\n--- неузнанный филиал задним числом ---")
    print(f"  исправлено отзывов: {fixed}  (ждём 1)")
    print(f"  филиал: {row['address'] if row else 'НЕТ'}  (ждём пер. Ногина, 48)")
    print(f"  предупреждение снято: {not (row and row['warnings'])}")
    print(f"  повторный запуск: {storage.rematch_unassigned(conn)}  (ждём 0)")
    conn.close()


if __name__ == "__main__":
    test_rematch()
