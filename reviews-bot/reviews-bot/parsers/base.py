"""
Общее для всех парсеров: модель отзыва, ошибка разбора, ключ дедупликации.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Review:
    """Один отзыв, вытащенный из письма площадки."""

    source: str = "yandex"  # yandex | 2gis | tripadvisor

    # Филиал
    branch_name: str | None = None
    branch_address: str | None = None
    branch_id: str | None = None  # ID филиала на площадке, если виден

    # Содержание
    rating: int | None = None  # None = не удалось определить
    rating_confident: bool = False  # False = оценку стоит перепроверить руками
    text: str = ""
    text_truncated: bool = False
    author: str | None = None  # у Яндекса в письме нет, у 2ГИС есть

    # Ссылки
    reply_url: str | None = None

    # Служебное
    received_at: datetime | None = None  # дата письма, НЕ дата публикации отзыва
    message_id: str | None = None
    dedup_key: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.received_at:
            d["received_at"] = self.received_at.isoformat()
        return d


class ParseError(Exception):
    """Письмо не удалось разобрать. Письмо не теряем, передаём резервному разбору."""


def make_dedup_key(review: Review) -> str:
    """
    ID отзыва площадки в письмах не дают, поэтому ключ строим из того, что есть:
    площадка + филиал + оценка + текст, и автор, если он известен.

    Автор добавляется в ключ только когда он есть: так ключи Яндекса (где автора
    нет) не меняются, а у 2ГИС ключ получается заметно надёжнее.

    Совпадение ключа НЕ означает дубль наверняка — поэтому при совпадении отзыв
    не выбрасывается, а помечается «возможный дубль» (см. storage.save_review).
    """
    branch = (review.branch_id or review.branch_address or "").strip().lower()
    text = re.sub(r"\s+", " ", review.text).strip().lower()
    rating = str(review.rating if review.rating is not None else "?")

    raw = f"{review.source}|{branch}|{rating}|{text}"
    if review.author:
        raw += f"|{review.author.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
