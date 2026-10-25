"""
Реестр парсеров. IMAP-читалка по отправителю письма выбирает нужный.
Новая площадка = новый модуль в этой папке + строка в PARSERS.
"""

from parsers import gis2_business, yandex_business
from parsers.base import ParseError, Review, make_dedup_key

PARSERS = [yandex_business, gis2_business]


def pick(msg_from: str, subject: str = ""):
    """Вернуть модуль-парсер для письма или None, если письмо не от площадки."""
    for parser in PARSERS:
        if parser.matches(msg_from, subject):
            return parser
    return None


__all__ = ["PARSERS", "pick", "ParseError", "Review", "make_dedup_key"]
