"""
Вопрос, на который бот ждёт текстовый ответ (поиск, свой период, полный
текст, итоговый ответ на отзыв).

В групповом чате Telegram по умолчанию не показывает боту обычные
сообщения — только команды и ответы на сообщения бота. Поэтому вопрос
отправляется с ForceReply: у сотрудника сразу открывается «ответ» на
сообщение бота, и текст гарантированно доходит. selective + упоминание —
чтобы поле ответа открылось только у того, кто нажал кнопку, а не у всех.
"""

from __future__ import annotations

import html

from aiogram.types import ForceReply


def mention(user) -> str:
    if user is None:
        return ""
    name = html.escape(user.full_name or user.username or "коллега")
    return f'<a href="tg://user?id={user.id}">{name}</a>, '


async def ask(message, user, text: str, placeholder: str = "", reply: bool = False):
    """message — куда отправить (сообщение бота или пользователя), user — кого спрашиваем."""
    markup = ForceReply(selective=True, input_field_placeholder=placeholder[:64] or None)
    body = mention(user) + text
    if reply:
        return await message.reply(body, reply_markup=markup)
    return await message.answer(body, reply_markup=markup)
