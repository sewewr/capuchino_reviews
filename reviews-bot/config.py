"""
Настройки. Всё берётся из переменных окружения — на Railway они задаются
в разделе Variables, локально можно положить в файл .env.

В репозиторий пароли не кладём никогда.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"не задана переменная окружения {name}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"переменная {name} должна быть числом, получено {raw!r}")


@dataclass(frozen=True)
class Config:
    # База. На Railway переменная DATABASE_URL появляется сама
    # после добавления PostgreSQL к проекту.
    database_url: str

    # Почтовый ящик, куда приходят уведомления площадок.
    imap_host: str
    imap_user: str
    imap_password: str  # пароль приложения, НЕ пароль от аккаунта
    imap_folder: str = "INBOX"
    imap_port: int = 993

    # С какой даты брать письма. В ящике может лежать годовой архив,
    # и при первом запуске бот не должен вывалить его в Telegram разом.
    imap_since: str = ""

    # Как часто проверяем почту, в секундах.
    poll_interval: int = 300

    # Telegram
    telegram_token: str = ""
    telegram_admin_chat: str = ""  # сюда шлём сообщения о сбоях
    reviews_chat: str = ""  # куда идут карточки отзывов («Все отзывы», п. 15 ТЗ)
    negative_chat: str = ""  # отдельный чат только для негатива (п. 15 ТЗ)

    # Google Таблица (п. 10 ТЗ). Если ключа нет, выгрузка просто не работает,
    # на сбор отзывов это не влияет.
    google_credentials: str = ""  # содержимое JSON-ключа сервисного аккаунта
    google_sheet_id: str = ""     # из ссылки на таблицу
    google_sheet_name: str = "Отзывы"

    # Часовой пояс. Без него сервер живёт по UTC и время в уведомлениях уедет.
    timezone: str = "Europe/Moscow"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=_env("DATABASE_URL", required=True),
            imap_host=_env("IMAP_HOST", "imap.mail.ru"),
            imap_user=_env("IMAP_USER", required=True),
            imap_password=_env("IMAP_PASSWORD", required=True),
            imap_folder=_env("IMAP_FOLDER", "INBOX"),
            imap_port=_env_int("IMAP_PORT", 993),
            imap_since=_env("IMAP_SINCE", ""),
            poll_interval=_env_int("POLL_INTERVAL", 300),
            telegram_token=_env("TELEGRAM_TOKEN", ""),
            telegram_admin_chat=_env("TELEGRAM_ADMIN_CHAT", ""),
            reviews_chat=_env("REVIEWS_CHAT_ID", ""),
            negative_chat=_env("NEGATIVE_CHAT_ID", ""),
            google_credentials=_env("GOOGLE_CREDENTIALS_JSON", ""),
            google_sheet_id=_env("GOOGLE_SHEET_ID", ""),
            google_sheet_name=_env("GOOGLE_SHEET_NAME", "Отзывы"),
            timezone=_env("TZ", "Europe/Moscow"),
        )
