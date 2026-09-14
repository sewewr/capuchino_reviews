"""
Прогон парсера на реальных письмах.

Использование:
    python test_parse.py путь/к/письму.eml [ещё письма...]

Без аргументов берёт все .eml из папки samples/.
"""

import glob
import sys
from pathlib import Path

from parsers import yandex_business


def show(path: str) -> None:
    print("=" * 78)
    print(Path(path).name)
    print("=" * 78)

    raw = Path(path).read_bytes()
    try:
        reviews = yandex_business.parse(raw)
    except yandex_business.ParseError as e:
        print(f"  НЕ РАЗОБРАНО: {e}")
        return

    print(f"  отзывов в письме: {len(reviews)}")
    for i, r in enumerate(reviews, 1):
        print(f"\n  --- отзыв {i} ---")
        print(f"  филиал:      {r.branch_name} / {r.branch_address}")
        print(f"  id филиала:  {r.branch_id or '— (спрятан за редиректом)'}")
        rating = r.rating if r.rating is not None else "?"
        mark = "" if r.rating_confident else "  (не уверен)"
        print(f"  оценка:      {rating}{mark}")
        print(f"  обрезан:     {'да' if r.text_truncated else 'нет'}")
        print(f"  письмо от:   {r.received_at}")
        print(f"  ключ дубля:  {r.dedup_key}")
        print(f"  текст:       {r.text[:90]}{'…' if len(r.text) > 90 else ''}")
        for w in r.warnings:
            print(f"  ! {w}")


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        paths = sorted(glob.glob("samples/*.eml")) or sorted(glob.glob("samples/*"))
    if not paths:
        print("нет писем для разбора: положи .eml в samples/")
        return
    for p in paths:
        show(p)


if __name__ == "__main__":
    main()
