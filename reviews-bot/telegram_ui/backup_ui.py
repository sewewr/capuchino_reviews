"""
Резервная копия в Telegram: /backup — сейчас, и раз в неделю сама,
в чат администратора (TELEGRAM_ADMIN_CHAT).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

import analytics
import backup
import storage

log = logging.getLogger(__name__)

router = Router(name="backup")

BACKUP_LAST = "backup_last"
BACKUP_DAY = 6          # воскресенье
BACKUP_AT = time(3, 0)  # ночью, когда никто не работает
TELEGRAM_LIMIT = 49 * 1024 * 1024


async def send_backup(bot: Bot, chat_id, conn) -> bool:
    data, counts = await asyncio.to_thread(backup.make_backup, conn)
    if len(data) > TELEGRAM_LIMIT:
        # оригиналы писем — самое тяжёлое; без них копия всё равно полная по отзывам
        data, counts = await asyncio.to_thread(backup.make_backup, conn, False)
        note = "\nОригиналы писем не вошли — копия иначе не помещается в Telegram."
    else:
        note = ""
    stamp = datetime.now(analytics.TZ).strftime("%Y-%m-%d")
    await bot.send_document(
        chat_id,
        BufferedInputFile(data, f"reviews-backup-{stamp}.zip"),
        caption=(f"💾 Резервная копия базы от {stamp}: {backup.describe(counts)}.{note}\n"
                 "Храните этот файл — по нему базу можно восстановить скриптом "
                 "restore_backup.py (инструкция в README)."),
    )
    return True


@router.message(Command("backup"))
async def cmd_backup(message: Message, conn) -> None:
    from telegram_ui.access import allow_message

    if not await allow_message(message):
        return
    wait = await message.answer("Готовлю копию…")
    try:
        await send_backup(message.bot, message.chat.id, conn)
    except Exception as e:
        log.exception("не сделал резервную копию")
        await message.answer(f"Не получилось сделать копию: {e}")
    try:
        await wait.delete()
    except Exception:
        pass


def backup_due(last: str | None, now: datetime) -> bool:
    local = now.astimezone(analytics.TZ)
    return (local.weekday() == BACKUP_DAY and local.time() >= BACKUP_AT
            and last != analytics.week_key(local))


async def backup_if_due(bot: Bot, conn, now: datetime | None = None) -> bool:
    chat = os.environ.get("TELEGRAM_ADMIN_CHAT", "")
    now = now or datetime.now(analytics.TZ)
    if not chat or not backup_due(storage.get_state(conn, BACKUP_LAST), now):
        return False
    storage.set_state(conn, BACKUP_LAST, analytics.week_key(now))
    try:
        await send_backup(bot, chat, conn)
        log.info("еженедельная резервная копия отправлена")
        return True
    except Exception:
        log.exception("еженедельная резервная копия не отправилась")
        try:
            await bot.send_message(chat, "⚠️ Не получилось отправить еженедельную резервную "
                                         "копию базы. Сделать вручную: /backup")
        except Exception:
            pass
        return False
