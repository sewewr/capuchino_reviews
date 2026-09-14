"""
Справочник филиалов сети «Капучино».

Адреса записаны так, как они указаны в карточках площадок. Написание не обязано
совпадать с тем, что придёт в письме: сопоставление устойчиво к сокращениям,
порядку слов и лишним уточнениям вроде района.

chat_id заполняется позже, когда будут созданы чаты в Telegram. Пока None —
уведомления идут в общий чат.

Запуск (создаёт или обновляет филиалы, существующие не дублируются):
    DATABASE_URL=postgresql://... python seed_branches.py
"""

from __future__ import annotations

import os

import storage

BRANCHES = [
    {"name": "Капучино", "address": "пер. Ногина, 48, Волгоград"},
    {"name": "Капучино", "address": "Краснознаменская ул., 9, Волгоград"},
    {"name": "Капучино", "address": "ул. Рокоссовского, 54, Волгоград"},
    {"name": "Капучино", "address": "просп. имени В.И. Ленина, 54Б, Волгоград"},
    {"name": "Капучино", "address": "бул. 30-летия Победы, 21, район Семь Ветров, Волгоград"},
    {"name": "Капучино", "address": "ул. Николая Отрады, 26, Волгоград"},
    {"name": "Капучино", "address": "просп. Маршала Жукова, 117, Волгоград"},
    {"name": "Капучино", "address": "Рабоче-Крестьянская ул., 9Б, Волгоград"},
    {"name": "Капучино", "address": "ул. Генерала Штеменко, 5, Волгоград"},
    {"name": "Капучино", "address": "Университетский просп., 107, Волгоград"},
]

# Пока подключаем только Яндекс. Когда придут письма 2ГИС, добавим "2gis"
# — адреса в карточках 2ГИС могут отличаться, поэтому филиалы заводятся
# отдельно для каждой площадки.
SOURCES = ["yandex"]


def main() -> None:
    conn = storage.connect(os.environ["DATABASE_URL"])
    storage.init_schema(conn)

    for source in SOURCES:
        for branch in BRANCHES:
            bid = storage.upsert_branch(
                conn, source, branch["name"], branch["address"],
                chat_id=branch.get("chat_id"),
            )
            print(f"  [{source}] id={bid}  {branch['address']}")
    conn.commit()

    check_collisions()
    conn.close()


def check_collisions() -> None:
    """
    Проверка, что адреса филиалов различимы после нормализации.
    Если два филиала сводятся к одинаковому набору слов, бот не сможет
    их различить, и это надо чинить до запуска, а не после.
    """
    seen: dict[str, str] = {}
    problems = 0
    for branch in BRANCHES:
        key = storage.normalize_address(branch["address"])
        if key in seen:
            print(f"  КОНФЛИКТ: {branch['address']} и {seen[key]} неразличимы")
            problems += 1
        seen[key] = branch["address"]
    print("  конфликтов адресов нет" if not problems else f"  конфликтов: {problems}")


if __name__ == "__main__":
    main()
