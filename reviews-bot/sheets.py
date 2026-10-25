"""
Выгрузка отзывов в Google Таблицу (п. 10 ТЗ).

Таблица — витрина, а не хранилище: источник правды остаётся в PostgreSQL,
в таблицу данные только выгружаются. Поэтому лист переписывается целиком,
а не дополняется построчно — так статусы и добранные вручную поля
(полный текст, автор, дата публикации) всегда совпадают с базой.

Чтобы не дёргать Google почём зря, перед записью считается отпечаток данных:
если с прошлого раза ничего не изменилось, обращения к API не происходит.

Ключ сервисного аккаунта берётся из переменной окружения целиком, как JSON,
а не файлом — так его не надо класть в репозиторий.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Moscow")

# Колонки по п. 11 ТЗ, в том же порядке.
HEADERS = [
    "Дата",
    "Площадка",
    "Филиал",
    "Автор",
    "Оценка",
    "Отзыв",
    "Ссылка",
    "Статус",
    "Ответственный",
    "Ответ",
]

SOURCE_NAMES = {
    "yandex": "Яндекс Карты",
    "2gis": "2ГИС",
    "tripadvisor": "Tripadvisor",
}

STATUS_NAMES = {
    "new": "Новый",
    "attention": "Требует внимания",
    "in_progress": "В работе",
    "answered": "Обработан",
    "skipped": "Не требует ответа",
}

# Google ограничивает длину содержимого ячейки; отзывы столько не занимают,
# но обрезаем на всякий случай, чтобы выгрузка не падала целиком.
CELL_LIMIT = 45_000

STATE_KEY = "sheets_fingerprint"


class SheetsError(Exception):
    """Не удалось выгрузить. Бот продолжает работу, отзывы остаются в базе."""


@dataclass
class ExportResult:
    rows: int
    written: bool  # False = данные не изменились, обращения к Google не было


# --- Подготовка данных ------------------------------------------------------


def fetch_rows(conn) -> list[list[str]]:
    """Все отзывы из базы в виде строк таблицы, новые сверху."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.received_at, r.published_at, r.source, r.rating,
                   r.text, r.text_full, r.text_truncated,
                   r.author, r.reply_url, r.status,
                   b.name AS branch_name, b.address AS branch_address,
                   r.branch_address_raw
            FROM reviews r
            LEFT JOIN branches b ON b.id = r.branch_id
            ORDER BY r.received_at DESC, r.id DESC
            """
        )
        return [_row(record) for record in cur.fetchall()]


def _row(rec) -> list[str]:
    # Дата публикации точнее даты письма, но у площадок её обычно нет,
    # поэтому показываем ту, что есть, и помечаем приблизительную.
    if rec["published_at"]:
        date = rec["published_at"].strftime("%d.%m.%Y")
    else:
        local = rec["received_at"].astimezone(TZ)
        date = f"{local:%d.%m.%Y %H:%M} (получено)"

    branch = rec["branch_name"] or "—"
    address = rec["branch_address"] or rec["branch_address_raw"]
    if address:
        branch = f"{branch}, {_short_address(address)}"

    text = rec["text_full"] or rec["text"] or ""
    if rec["text_truncated"] and not rec["text_full"]:
        text = f"{text} [текст обрезан площадкой]"

    return [
        date,
        SOURCE_NAMES.get(rec["source"], rec["source"]),
        branch,
        rec["author"] or "",
        str(rec["rating"]) if rec["rating"] is not None else "",
        text[:CELL_LIMIT],
        rec["reply_url"] or "",
        STATUS_NAMES.get(rec["status"], rec["status"]),
        "",  # Ответственный — этап 2 по ТЗ
        "",  # Ответ — этап 2 по ТЗ
    ]


def _short_address(address: str) -> str:
    """Город в таблице не нужен — все филиалы в одном."""
    parts = [p.strip() for p in address.split(",")]
    parts = [p for p in parts if p.lower() not in ("волгоград", "россия")]
    return ", ".join(parts) if parts else address


def fingerprint(rows: list[list[str]]) -> str:
    raw = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


# --- Выгрузка ---------------------------------------------------------------


def open_sheet(credentials_json: str, sheet_id: str, worksheet: str):
    """
    Подключение к таблице. Лист создаётся, если его ещё нет.
    Вынесено отдельно, чтобы в тестах подставить заглушку.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    try:
        info = json.loads(credentials_json)
    except json.JSONDecodeError as e:
        raise SheetsError(f"GOOGLE_CREDENTIALS_JSON — не JSON: {e}") from e

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        book = client.open_by_key(sheet_id)
    except Exception as e:
        raise SheetsError(f"не открылась таблица: {e}") from e

    try:
        return book.worksheet(worksheet)
    except Exception:
        try:
            return book.add_worksheet(worksheet, rows=100, cols=len(HEADERS))
        except Exception as e:
            raise SheetsError(f"не создался лист «{worksheet}»: {e}") from e


def write_rows(sheet, rows: list[list[str]]) -> None:
    """
    Переписываем лист целиком: шапка плюс данные.

    clear() перед записью нужен, потому что иначе после удаления отзывов
    в конце листа остались бы старые строки.
    """
    try:
        sheet.clear()
        sheet.update(values=[HEADERS] + rows, range_name="A1")
        sheet.freeze(rows=1)
    except Exception as e:
        raise SheetsError(f"не записались данные: {e}") from e


def export(conn, credentials_json: str, sheet_id: str, worksheet: str = "Отзывы",
           opener=open_sheet, writer=write_rows) -> ExportResult:
    """
    Выгрузить все отзывы в таблицу. Если данные не изменились с прошлого
    раза, к Google не обращаемся.

    opener и writer параметризованы ради тестов — в бою используются
    значения по умолчанию.
    """
    import storage

    rows = fetch_rows(conn)
    mark = fingerprint(rows)

    if storage.get_state(conn, STATE_KEY) == mark:
        return ExportResult(rows=len(rows), written=False)

    sheet = opener(credentials_json, sheet_id, worksheet)
    writer(sheet, rows)
    storage.set_state(conn, STATE_KEY, mark)

    logger.info("выгружено в Google Таблицу строк: %s", len(rows))
    return ExportResult(rows=len(rows), written=True)
