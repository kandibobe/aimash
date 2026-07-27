"""Политика вложения к черновику: ОБЕЩАТЬ ли .xlsx и что в нём будет. Без openpyxl, ФС и aiogram.

Модуль закрывает конкретный живой дефект, а не абстрактную нужду. `confirm/render.py` печатает
человеку «…ещё N — полный список во вложении .xlsx», и по этому тексту менеджер жмёт «да». Набор
операций, для которых файл РЕАЛЬНО отправлялся, задавался отдельно и в другом месте
(`bot/main.py::_KEYWORD_OPS`), причём `add_negatives_to_shared_set` в нём не было вовсе: обещание
печаталось, файл не уходил, а текст обещания уезжал в `summary` черновика — то есть в audit-row,
из которого правило 15 репортит «выполнено». Два источника истины на один вопрос — здесь один.

**Почему отдельный модуль, а не место в существующем** (причина жёсткая, не вкусовая): решение
обязаны читать ТРИ процесса — тул-слой (обещать ли), рендер (напечатать обещание) и планировщик
(доставить). `keywords/export.py` тянет openpyxl на уровне модуля, и политика в нём затащила бы
openpyxl в MCP-процесс, который стартует по `docker exec -i aimash-bot python -m mcp_server` и уже
упирался в `mcp_discovery_timeout` на холодном старте. `confirm/render.py` — форматтер, чей узкий
контракт («как показать», возврат `str`) приколочен headless-гардом. `confirm/gate.py` — контейнер
данных.

Направление зависимости одно: политика → рендер (порог `KW_INLINE_MAX`) и `core.texts` (подписи).
Обратно никто не смотрит, поэтому цикла нет по построению.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Модуль целиком, а не `from … import KW_INLINE_MAX`: имя, импортированное значением, — это КОПИЯ
# порога, снятая в момент импорта. Одного источника истины на два решения (усечь / приложить) при
# такой связи нет — есть два числа, которые сегодня совпадают. Через атрибут связь настоящая.
from confirm import render
from confirm.render import match_type_human

# Операции, чей список ключей/минус-слов уходит .xlsx-вложением (ТЗ §5: большие списки — вложением,
# не текстом). ЕДИНСТВЕННЫЙ источник истины: `confirm/render.py` печатает обещание по тому же
# набору, `bot/main.py` и планировщик доставляют по нему же. Раньше набор жил в кнопочном слое и
# расходился с рендером — операцию добавляли в один список и забывали во втором.
KEYWORD_XLSX_OPS: frozenset[str] = frozenset(
    {
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
        # Разрыв, ради которого модуль и появился: рендер обещает вложение (confirm/render.py:453),
        # а в кнопочном `_KEYWORD_OPS` операции не было — обещание печаталось, файл не уходил.
        "add_negatives_to_shared_set",
        # Финальный гейт создания кампании тоже несёт список ключей (C4, аудит 2026-07).
        "create_search_campaign",
    }
)

# Потолок строк листа. Не «красивое число»: список приходит из текста менеджера/модели, потолка на
# него нет нигде, а `send_document` Telegram упирается в 50 МБ — без потолка вставленный из CSV
# список на 200k минус-слов даёт обещание без файла (ровно тот дефект, который модуль чинит).
# Превышение не молчит: лист несёт пометку об усечении, спека — полное `total`.
MAX_ATTACHMENT_ROWS = 5000

_FILENAME_BAD = re.compile(r"[^a-z0-9.-]+")


def safe_filename(stem: str) -> str:
    """Санитайзер имени файла для Telegram-вложения: только `[a-z0-9.-]`, без ведущих/хвостовых
    точек и подчёркиваний. Пустой/полностью небезопасный вход → `attachment` (никогда '' и никогда
    путь: `../` схлопывается в `_`, разделители каталогов не выживают)."""
    safe = _FILENAME_BAD.sub("_", (stem or "").lower()).strip("_.")
    return safe[:64] or "attachment"


@dataclass(frozen=True, slots=True)
class AttachmentSpec:
    """ЧТО приложить. Чистые данные: ни путей, ни байтов, ни chat_id — их знает доставка."""

    kind: str  # "keywords_xlsx"; точка расширения под график L3 (Волна 5)
    keywords: tuple[str, ...]
    match_type: str  # уже человекочитаемый (render.match_type_human)
    action: str  # core.texts.keyword_action_label
    scope: str  # кампания или общий набор — лист самодостаточен без карточки
    filename: str
    total: int  # ДО усечения по MAX_ATTACHMENT_ROWS


def plan_attachment(operation: str, params: dict, *, cid: str, lang: str) -> AttachmentSpec | None:
    """Решить, нужно ли вложение, и собрать его спеку. `None` = не нужно (и обещать нельзя).

    Чистая функция от (operation, params): и тот, кто печатает обещание, и тот, кто через минуту
    доставляет файл из ПЕРСИСТЕНТНОЙ строки черновика, зовут её и получают одно и то же. Разъехаться
    им не на чем — нет второго места, где решение принимается.

    Порог — `confirm.render.KW_INLINE_MAX`, тот самый, по которому рендер усекает список. Своей
    константы здесь нет намеренно: два порога разошлись бы в первый же день (обещание про «ещё 3»
    без файла, либо файл без обещания)."""
    if operation not in KEYWORD_XLSX_OPS or not isinstance(params, dict):
        return None
    kws = params.get("keywords")
    if not isinstance(kws, (list, tuple)):
        return None
    rows = tuple(str(k) for k in kws)
    if len(rows) <= render.KW_INLINE_MAX:
        return None  # список целиком виден в карточке — вложение было бы шумом
    # Фолбэк на общий набор: у add_negatives_to_shared_set кампании нет вовсе, и без этой колонки
    # лист не отвечал бы на вопрос «куда это ляжет».
    scope = str(params.get("campaign") or params.get("shared_set") or "")
    from core.texts import keyword_action_label  # ленивый: core.texts тянет весь каталог RU/EN

    return AttachmentSpec(
        kind="keywords_xlsx",
        keywords=rows,
        match_type=match_type_human(str(params.get("match_type") or ""), lang),
        action=keyword_action_label(operation, lang),
        scope=scope,
        # cid в имени: два черновика одной операции не должны давать одинаковый файл — в чате они
        # неразличимы, а менеджер решает по тому, который открыл.
        filename=f"{safe_filename(operation)}_{safe_filename(cid)[:12]}.xlsx",
        total=len(rows),
    )
