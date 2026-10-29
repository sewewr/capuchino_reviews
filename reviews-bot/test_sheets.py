"""
Проверка выгрузки в Google Таблицу без обращения к Google.

Настоящий аккаунт здесь не нужен: подставляем заглушку вместо листа
и смотрим, что именно бот записал бы.

    DATABASE_URL=postgresql://... python test_sheets.py
"""

import os
from datetime import datetime, timedelta

import sheets
import storage
from parsers.base import Review, make_dedup_key
from sheets import TZ


class FakeSheet:
    """Лист-заглушка: запоминает, что в него записали."""

    def __init__(self):
        self.rows = None
        self.cleared = 0
        self.frozen = None

    def clear(self):
        self.cleared += 1

    def update(self, values, range_name):
        self.rows = values

    def freeze(self, rows):
        self.frozen = rows


def setup(conn):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS drafts, review_messages, reviews, emails, branches, state CASCADE")
    conn.commit()
    storage.init_schema(conn)

    from seed_branches import BRANCHES, SOURCES

    for source in SOURCES:
        for b in BRANCHES:
            storage.upsert_branch(conn, source, b["name"], b["address"])
    conn.commit()


def add(conn, rating, text, source="yandex", address="Волгоград, улица Рокоссовского, 54",
        author=None, truncated=False, hours_ago=1):
    r = Review(
        source=source, branch_name="Капучино", branch_address=address,
        rating=rating, rating_confident=True, text=text, author=author,
        text_truncated=truncated,
        reply_url="https://yandex.ru/sprav/123/p/edit/reviews",
        received_at=datetime.now(TZ) - timedelta(hours=hours_ago),
    )
    r.dedup_key = make_dedup_key(r)
    return storage.save_review(conn, r)


def main():
    conn = storage.connect(os.environ["DATABASE_URL"])
    setup(conn)

    add(conn, 5, "Отличное кафе, вкусные десерты", hours_ago=3)
    add(conn, 2, "Ждали заказ 40 минут", hours_ago=2)
    add(conn, 5, "Все круто", source="2gis", author="Артём Науменко",
        address="Волгоград, 30-летия Победы бульвар, 21", hours_ago=1)
    long_one = add(conn, 4, "Моё самое любимое место, как сладкоежки, обожаю всё, оч...",
                   truncated=True, hours_ago=4)

    print("--- первая выгрузка ---")
    sheet = FakeSheet()
    res = sheets.export(conn, "{}", "sheet-id", opener=lambda *a: sheet,
                        writer=sheets.write_rows)
    print(f"  строк: {res.rows}, записано в Google: {res.written}")
    print(f"  лист очищен: {sheet.cleared} раз, шапка закреплена: {sheet.frozen}")

    print("\n--- что в таблице ---")
    head = sheet.rows[0]
    print("  " + " | ".join(head))
    for row in sheet.rows[1:]:
        cells = [c if len(c) <= 28 else c[:25] + "…" for c in row]
        print("  " + " | ".join(cells))

    print("\n--- повторная выгрузка без изменений ---")
    sheet2 = FakeSheet()
    res2 = sheets.export(conn, "{}", "sheet-id", opener=lambda *a: sheet2,
                         writer=sheets.write_rows)
    print(f"  записано в Google: {res2.written}  (ждём False — данные те же)")
    print(f"  лист тронут: {sheet2.cleared > 0}  (ждём False)")

    print("\n--- меняем статус отзыва ---")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reviews WHERE rating = 2")
        rid = cur.fetchone()["id"]
    storage.set_status(conn, rid, "answered")
    sheet3 = FakeSheet()
    res3 = sheets.export(conn, "{}", "sheet-id", opener=lambda *a: sheet3,
                         writer=sheets.write_rows)
    print(f"  записано в Google: {res3.written}  (ждём True)")
    changed = [r for r in sheet3.rows[1:] if r[4] == "2"]
    print(f"  статус в таблице: {changed[0][7]}")

    print("\n--- добор полного текста попадает в таблицу ---")
    storage.add_full_text(conn, long_one.review_id,
                          "Полный текст отзыва, вставленный сотрудником вручную",
                          author="Мария К.", published_at="2026-08-26")
    sheet4 = FakeSheet()
    sheets.export(conn, "{}", "sheet-id", opener=lambda *a: sheet4,
                  writer=sheets.write_rows)
    row = [r for r in sheet4.rows[1:] if r[3] == "Мария К."][0]
    print(f"  дата:   {row[0]}")
    print(f"  автор:  {row[3]}")
    print(f"  текст:  {row[5][:60]}")

    print("\n--- удалённый отзыв исчезает из таблицы ---")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reviews WHERE rating = 2")
    conn.commit()
    sheet5 = FakeSheet()
    res5 = sheets.export(conn, "{}", "sheet-id", opener=lambda *a: sheet5,
                         writer=sheets.write_rows)
    print(f"  строк осталось: {res5.rows}  (было 4)")

    print("\n--- ошибка Google не роняет бота ---")
    def broken_opener(*args):
        raise sheets.SheetsError("нет доступа к таблице")

    with conn.cursor() as cur:
        cur.execute("DELETE FROM state WHERE key = %s", (sheets.STATE_KEY,))
    conn.commit()
    try:
        sheets.export(conn, "{}", "sheet-id", opener=broken_opener)
        print("  ОШИБКА: исключение не поднялось")
    except sheets.SheetsError as e:
        print(f"  поймано как надо: {e}")

    print("\n--- кривой ключ сервисного аккаунта ---")
    try:
        sheets.open_sheet("это не json", "sheet-id", "Отзывы")
        print("  ОШИБКА: приняли мусор")
    except sheets.SheetsError as e:
        print(f"  поймано: {str(e)[:60]}")

    conn.close()


if __name__ == "__main__":
    main()
