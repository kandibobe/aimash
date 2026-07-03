"""FSM-состояния всех визардов бота (4A: вынос из bot/main.py — декомпозиция god-module).

Только StatesGroup-классы — без логики и зависимостей от main (разрывает часть bm.<name>-связки).
bot/main.py ре-импортирует все имена явно, поэтому хендлеры и monkeypatch тестов продолжают
обращаться к ним как bm.<Wizard> (позднее связывание сохранено).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ClientInfoWizard(StatesGroup):
    """§20: приём информации о клиенте текстом. FSM хранит только выбранный аккаунт {cli_customer_id}
    и режим {cli_mode: add|update}; накопленный текст — в _CLI_TEXT_BUF[chat_id] до «💾 Сохранить»."""

    awaiting_text = State()  # ждём текст(ы) профиля; накапливаем до Сохранить/Отмена


class RsaWizard(StatesGroup):
    picking = (
        State()
    )  # N5: показан пикер кампании/группы (кнопки) — текст тут не ждём (гард on_text)
    awaiting_brief = State()  # ждём «тематика | url» для генерации


class RsaRefine(StatesGroup):
    awaiting_text = State()  # ждём правку для доработки одного элемента


class RsaList(StatesGroup):
    awaiting_edited = State()  # §10 list-UX: ждём отредактированный СПИСОК заголовков/описаний


class KwWizard(StatesGroup):
    awaiting_seeds = State()  # ждём сид-слова и/или URL для подбора ключей
    params = State()  # 3F (§7): экран параметров research (ГЕО/язык/сеть/период)
    awaiting_geo = State()  # 3F: ручной ввод страны для ГЕО подбора


class KwAdd(StatesGroup):
    awaiting_campaign = State()  # §7: ждём название кампании для добавления подобранных ключей
    awaiting_keywords = State()  # §7 list-UX: ждём отредактированный СПИСОК ключей (правка+назад)


class Geo(StatesGroup):
    # §3: способ выбран в меню → ждём текст. campaign лежит в state-data (geo_campaign).
    awaiting_locations = State()  # ждём локации через запятую (страна/город/регион)
    awaiting_proximity = State()  # ждём «город, радиус_км» для радиус-таргетинга


class SearchWizard(StatesGroup):
    awaiting_brief = State()  # ждём «название | url | бюджет [| тематика [| ключи]]» (/newsearch)


class GdnWizard(StatesGroup):
    awaiting_brief = State()  # ждём «название | url | бюджет» после приёма фото


class VideoWizard(StatesGroup):
    """§11: кампания из видео (Demand Gen / Video). Видео живёт на YouTube — визард просит ссылку."""

    awaiting_link = State()  # ждём ссылку на YouTube (или 11-символьный id)
    awaiting_brief = State()  # ждём «название | url сайта | бюджет [| гео]» после выбора типа
    awaiting_logo = State()  # Demand Gen: ждём фото логотипа или «⏭ Пропустить»


class ModelWizard(StatesGroup):
    awaiting_model = State()  # ждём свой slug модели OpenRouter для /model


class AlertsWizard(StatesGroup):
    awaiting_value = State()  # 3H (M10): /alerts «✏️» — ждём число порога (field в state-data)


class TplWizard(StatesGroup):
    awaiting_name = State()  # §2B: создание из шаблона — ждём ИМЯ новой кампании (token в state)


class IngestWizard(StatesGroup):
    awaiting_task = (
        State()
    )  # ingest: файл принят без подписи → ждём задачу (контент в _PENDING_CONTEXT)


class ExtWizard(StatesGroup):
    # §3-assets: тип расширения выбран в меню → ждём текст/фото. Кампания в state-data (ext_campaign).
    awaiting_sitelinks = State()  # «Текст | url [| описание1 [| описание2]]» построчно
    awaiting_callouts = State()  # уточнения через запятую/строки
    awaiting_snippet_values = State()  # значения через запятую (header выбран кнопкой → ext_header)
    awaiting_image = State()  # ждём фото для image-ассета (перехват в on_photo по состоянию)


class CreateCampaignWizard(StatesGroup):
    """§19: guided-визард создания Search-кампании. FSM хранит только курсор {cc_session} —
    накопленный черновик живёт в campaign_drafts (переживает рестарт). 8 этапов: 0 аккаунт →
    1 настройки → 2 ключи → 3 объявление → 4 изображения → 5 ассеты → 6 URL-опции → 7 финал."""

    account_select = State()  # Этап 0: показан список аккаунтов, ждём выбор
    settings_desc = State()  # Этап 1: ждём свободное описание кампании
    settings_confirm = State()  # Этап 1: показаны настройки, ждём правку-текст или ✅
    keywords = State()  # Этап 2: ждём свои ключи (текст/файл/ссылка) ИЛИ кнопку «Генерация»
    kw_verify = State()  # Этап 2: ждём ссылку на отредактированную таблицу (round-trip)
    ad_url = State()  # Этап 3: ждём Final URL (далее курация RSA — callback-driven)
    images = State()  # Этап 4: ждём фото или «Пропустить»
    assets = State()  # Этап 5: выбор «текущие/добавить/пропустить» (callback-driven)
    asset_logo = State()  # Этап 5: выбран Business logo — ждём фото логотипа (1:1)
    url_options = State()  # Этап 6: ждём «tracking | suffix» или «Пропустить»
    final = State()  # Этап 7: сводка; ждём правку-текст или ✅ Создать / 🚀 Запустить
