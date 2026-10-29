"""
ИИ-черновики ответов на отзывы (п. 9 ТЗ) через GigaChat.

ИИ готовит только черновик. Публикует ответ человек: площадки не дают
отвечать через API, а у Яндекса ответ ещё и проходит модерацию.

Что учитывается в черновике (п. 9 ТЗ):
  — текст, оценка, филиал и тон бренда;
  — положительный отзыв: поблагодарить, отметить конкретную деталь, пригласить снова;
  — негативный: не спорить, поблагодарить за обратную связь, признать проблему,
    если для этого есть основания, предложить разобраться;
  — ответы не шаблонные и не начинаются одинаково: модели передаются начала
    последних черновиков, и повтор отсекается.

Ограничения площадок (правила модерации Яндекс Бизнеса): ответ на русском,
до 2500 знаков, без @-ссылок на Telegram, без слов капсом, без избытка эмодзи
и восклицательных знаков, без персональных данных.

Варианты (п. 9 ТЗ): другой вариант, короче, теплее, официальнее.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MAX_LENGTH = 2500  # предел Яндекса
RECENT_OPENINGS = 15  # сколько последних начал запрещать повторять

# Корневой сертификат НУЦ Минцифры: без него GigaChat не пускает по TLS.
# Ищем в репозитории, иначе скачиваем при первом обращении.
CERT_URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
CERT_IN_REPO = Path(__file__).parent / "certs" / "russian_trusted_root_ca.crt"

STYLES = {
    "base": "",
    "another": (
        "Напиши ДРУГОЙ вариант: другое начало, другое построение, другие "
        "слова. Не повторяй предыдущий вариант:\n«{previous}»"
    ),
    "shorter": (
        "Сделай ответ заметно короче — 1–2 предложения. Сохрани суть и "
        "обращение. Предыдущий вариант:\n«{previous}»"
    ),
    "warmer": (
        "Сделай ответ теплее и душевнее, по-домашнему, но без приторности. "
        "Предыдущий вариант:\n«{previous}»"
    ),
    "formal": (
        "Сделай ответ официальнее и сдержаннее, без эмодзи. "
        "Предыдущий вариант:\n«{previous}»"
    ),
}

STYLE_TEMPERATURE = {"base": 0.7, "another": 1.0, "shorter": 0.5,
                     "warmer": 0.8, "formal": 0.5}

SOURCE_NAMES = {"yandex": "Яндекс Карты", "2gis": "2ГИС", "tripadvisor": "Tripadvisor"}


class DraftError(Exception):
    """Черновик не получился. Отзыв при этом никуда не девается."""


@dataclass
class ReviewForDraft:
    review_id: int
    source: str
    rating: int | None
    text: str
    text_truncated: bool
    author: str | None
    branch_address: str | None
    is_negative: bool


@dataclass
class Draft:
    text: str
    style: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class Brand:
    """Настройки голоса бренда. Всё задаётся переменными окружения."""

    name: str = "Капучино"
    kind: str = "сеть кафе-кондитерских в Волгограде"
    contact: str = ""  # телефон или почта для негатива; пусто — модель не выдумывает
    allow_compensation: bool = False  # можно ли предлагать возврат/угощение

    @classmethod
    def from_env(cls) -> "Brand":
        return cls(
            name=os.environ.get("BRAND_NAME", "Капучино"),
            kind=os.environ.get("BRAND_KIND", "сеть кафе-кондитерских в Волгограде"),
            contact=os.environ.get("REPLY_CONTACT", ""),
            allow_compensation=os.environ.get("REPLY_ALLOW_COMPENSATION", "").lower()
            in ("1", "true", "yes", "да"),
        )


# --- Промпт -----------------------------------------------------------------


def system_prompt(brand: Brand, recent_openings: list[str]) -> str:
    rules = [
        f"Ты пишешь ответы на отзывы гостей от имени «{brand.name}» — {brand.kind}.",
        "Пиши живо и по-человечески, как сотрудник, которому не всё равно, "
        "а не как служба поддержки. К гостю — на «Вы» с большой буквы.",
        "Если известно имя гостя, обратись по имени.",
        "",
        "Положительный отзыв: поблагодари, отметь конкретную деталь из отзыва "
        "(блюдо, напиток, сотрудника, атмосферу — то, что гость сам упомянул) "
        "и пригласи снова.",
        "Негативный отзыв: не спорь и не оправдывайся, поблагодари за обратную "
        "связь, признай проблему, если гость описал её конкретно, извинись за "
        "неудобство и предложи разобраться.",
        "Нейтральный отзыв: поблагодари, отметь хорошее и коротко ответь на замечание.",
        "",
        "Нельзя:",
        "— выдумывать факты: блюда, акции, имена сотрудников, причины проблемы;",
        "— писать шаблонно и начинать с дежурных фраз вроде «Благодарим за ваш отзыв»;",
        "— использовать слова целиком заглавными буквами, транслит, разрядку;",
        "— ставить больше двух эмодзи и больше одного восклицательного знака подряд;",
        "— оставлять ссылки на Telegram в формате @логин и любые персональные данные;",
        "— упоминать, что ответ написан ИИ.",
    ]

    if brand.contact:
        rules.append(
            f"Для негативного отзыва предложи связаться: {brand.contact}. "
            "Других контактов не указывай."
        )
    else:
        rules.append(
            "Не указывай телефоны, почту и ссылки — их нет. Для негатива просто "
            "предложи разобраться в ситуации."
        )

    if brand.allow_compensation:
        rules.append(
            "Если проблема явно по вине кафе, можно предложить загладить вину "
            "при следующем визите, без конкретных сумм."
        )
    else:
        rules.append("Не обещай компенсаций, скидок, возврата денег и подарков.")

    rules += [
        "",
        "Длина: обычно 2–4 предложения, не больше 600 знаков.",
        "Язык — только русский.",
        "Выведи только текст ответа: без кавычек, заголовков и пояснений.",
    ]

    if recent_openings:
        rules.append("")
        rules.append("Последние ответы начинались так — НЕ начинай похоже:")
        rules += [f"— {o}" for o in recent_openings]

    return "\n".join(rules)


def user_prompt(review: ReviewForDraft, style: str, previous: str | None) -> str:
    parts = [
        f"Площадка: {SOURCE_NAMES.get(review.source, review.source)}",
        f"Филиал: {review.branch_address or 'не определён'}",
        f"Оценка: {review.rating if review.rating is not None else 'неизвестна'} из 5",
    ]
    if review.author:
        parts.append(f"Имя гостя: {review.author}")
    text = review.text.strip() or "(гость поставил оценку без текста)"
    parts.append(f"Отзыв: «{text}»")
    if review.text_truncated:
        parts.append(
            "Внимание: текст отзыва обрезан площадкой. Не додумывай, что было дальше, "
            "отвечай только на то, что видно."
        )

    instruction = STYLES.get(style, "")
    if instruction:
        parts += ["", instruction.format(previous=previous or "")]
    else:
        parts += ["", "Напиши ответ на этот отзыв."]
    return "\n".join(parts)


# --- Проверка результата ----------------------------------------------------


_CAPS_WORD = re.compile(r"\b[А-ЯЁA-Z]{4,}\b")
_TG_HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{4,}|t\.me/\S+", re.IGNORECASE)
_ALLOWED_CAPS = {"КБЖУ", "ВКонтакте"}


def clean(text: str) -> str:
    """Убираем то, что модели иногда добавляют сверх ответа."""
    t = text.strip()
    t = re.sub(r"^(ответ|черновик|вариант)\s*[:\-—]\s*", "", t, flags=re.IGNORECASE)
    if len(t) >= 2 and t[0] in "«\"'" and t[-1] in "»\"'":
        t = t[1:-1].strip()
    return t


def check(text: str, brand: Brand) -> tuple[str, list[str]]:
    """
    Приводим к правилам площадок. Что можно исправить — исправляем,
    остальное возвращаем предупреждением: человек всё равно перечитает.
    """
    warnings: list[str] = []

    if _TG_HANDLE.search(text):
        text = _TG_HANDLE.sub("", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        warnings.append("убрана ссылка на Telegram — Яндекс такие не пропускает")

    text = re.sub(r"!{2,}", "!", text)

    caps = [w for w in _CAPS_WORD.findall(text) if w not in _ALLOWED_CAPS
            and w != brand.name.upper()]
    if caps:
        warnings.append(f"слова капсом ({', '.join(caps[:3])}) — модерация может отклонить")

    if len(text) > MAX_LENGTH:
        cut = text[:MAX_LENGTH]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > 0 else cut
        warnings.append(f"ответ сокращён до {MAX_LENGTH} знаков — лимит Яндекса")

    if re.search(r"[a-zA-Z]{6,}", text):
        warnings.append("в тексте латиница — проверьте, нет ли транслита")

    return text, warnings


def opening(text: str) -> str:
    """Начало ответа — первые слова первого предложения."""
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return " ".join(first.split()[:6])


def _same_start(a: str, b: str, words: int = 3) -> bool:
    norm = lambda s: [w.strip(",.!?—").lower() for w in s.split()][:words]  # noqa: E731
    return norm(a) == norm(b) and len(norm(a)) == words


# --- Клиент GigaChat ----------------------------------------------------------


_client = None


def ensure_certificate() -> str:
    """
    Путь к корневому сертификату Минцифры. Сначала ищем в репозитории
    (certs/russian_trusted_root_ca.crt), потом скачиваем во временную папку.
    """
    if CERT_IN_REPO.exists():
        return str(CERT_IN_REPO)

    target = Path(tempfile.gettempdir()) / "russian_trusted_root_ca.crt"
    if target.exists() and b"BEGIN CERTIFICATE" in target.read_bytes():
        return str(target)

    try:
        with urllib.request.urlopen(CERT_URL, timeout=20) as resp:
            data = resp.read()
    except Exception as e:
        raise DraftError(
            "не скачался сертификат Минцифры, без него GigaChat недоступен. "
            f"Положите файл с {CERT_URL} в папку certs проекта под именем "
            f"russian_trusted_root_ca.crt. Ошибка: {e}"
        ) from e

    if b"BEGIN CERTIFICATE" not in data:
        raise DraftError("скачанный сертификат Минцифры повреждён")
    target.write_bytes(data)
    return str(target)


def get_client():
    """Клиент создаётся один раз: он сам хранит и обновляет токен доступа."""
    global _client
    if _client is not None:
        return _client

    credentials = os.environ.get("GIGACHAT_CREDENTIALS", "")
    if not credentials:
        raise DraftError("не задан GIGACHAT_CREDENTIALS — черновики выключены")

    from gigachat import GigaChat

    _client = GigaChat(
        credentials=credentials,
        scope=os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        model=os.environ.get("GIGACHAT_MODEL", "GigaChat"),
        ca_bundle_file=ensure_certificate(),
        timeout=60,
    )
    return _client


def enabled() -> bool:
    return bool(os.environ.get("GIGACHAT_CREDENTIALS"))


def _ask(client, system: str, user: str, temperature: float) -> str:
    from gigachat.models import Chat, Messages, MessagesRole

    chat = Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=system),
            Messages(role=MessagesRole.USER, content=user),
        ],
        temperature=temperature,
        max_tokens=600,
    )
    try:
        response = client.chat(chat)
    except Exception as e:
        raise DraftError(f"GigaChat не ответил: {e}") from e

    if not response.choices:
        raise DraftError("GigaChat вернул пустой ответ")
    return response.choices[0].message.content or ""


# --- Главная функция ----------------------------------------------------------


def generate(review: ReviewForDraft, style: str = "base", previous: str | None = None,
             recent_openings: list[str] | None = None, brand: Brand | None = None,
             client=None) -> Draft:
    """
    Подготовить черновик. client подставляется в тестах; в бою берётся GigaChat.
    Если модель начала так же, как недавний ответ, просим ещё раз.
    """
    if style not in STYLES:
        raise DraftError(f"неизвестный вариант: {style}")
    if style != "base" and not previous:
        raise DraftError("для этого варианта нужен предыдущий черновик")

    brand = brand or Brand.from_env()
    recent = recent_openings or []
    client = client or get_client()
    temperature = STYLE_TEMPERATURE[style]

    system = system_prompt(brand, recent)
    user = user_prompt(review, style, previous)

    text = clean(_ask(client, system, user, temperature))
    if any(_same_start(text, o) for o in recent):
        log.info("черновик начался как недавний ответ — прошу ещё раз")
        retry = clean(_ask(client, system, user + "\n\nНачни ответ иначе, чем обычно.",
                           min(temperature + 0.3, 1.2)))
        if retry:
            text = retry

    if not text:
        raise DraftError("GigaChat вернул пустой текст")

    text, warnings = check(text, brand)
    if review.text_truncated:
        warnings.insert(0, "текст отзыва обрезан площадкой — черновик по фрагменту; "
                           "добавьте полный текст, чтобы ответ был точнее")
    return Draft(text=text, style=style, warnings=warnings)
