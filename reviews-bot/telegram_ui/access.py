"""
Кто может менять настройки (пп. 16, 21 ТЗ: «административные функции
доступны только разрешённым Telegram ID»).

ADMIN_IDS — Telegram ID администраторов через запятую. Узнать свой ID:
команда /id в личке с ботом.

Пока ADMIN_IDS не задан, ограничений нет — как было до появления ролей.
Так бот не «запирается» сам от себя при обновлении: список вписывается
в Railway при передаче проекта, и с этого момента настройки закрыты.

Сотрудникам доступно всё для работы с отзывами: карточки, черновики,
списки, поиск, аналитика, отчёты. Администратору дополнительно — порог
негатива, расписание и чат отчётов, резервная копия.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

DENIED = "Это могут только администраторы бота."


def admin_ids() -> set[int]:
    out = set()
    for part in os.environ.get("ADMIN_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("ADMIN_IDS: «%s» — не число, пропускаю", part)
    return out


def restricted() -> bool:
    return bool(admin_ids())


def is_admin(user) -> bool:
    ids = admin_ids()
    if not ids:
        return True
    return user is not None and getattr(user, "id", None) in ids


async def allow_message(message) -> bool:
    """Для команд: если нельзя — коротко отвечаем и возвращаем False."""
    if is_admin(getattr(message, "from_user", None)):
        return True
    await message.answer(DENIED)
    return False


async def allow_callback(callback) -> bool:
    """Для кнопок: всплывающее окно вместо сообщения в общий чат."""
    if is_admin(callback.from_user):
        return True
    await callback.answer(DENIED, show_alert=True)
    return False
