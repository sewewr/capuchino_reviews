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
        cur.execute("DROP TABLE IF EXISTS reviews, emails, branches CASCADE")
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
