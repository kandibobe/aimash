"""3.6 (а/в/г): карточка клиента секциями + группировка кнопок + нормализация гео из аккаунта.

Что стережётся:
- (а) fmt_client_card: секции вместо плоской простыни, услуги по одной на строку, длинные тела
  режутся _clip(_CARD_BODY_MAX); подсказка «полный текст — в Досье» ТОЛЬКО когда что-то реально
  обрезано И досье есть; esc() на всех значениях извне (профиль собран из краула — файл извне);
- (г) client_card_kb: те же callback_data (actions/sub нетронуты — хендлеры не правились),
  изменилась только раскладка рядами (adjust);
- (в) fetch_account_facts: «Kenya» из geo constants и «Кения» из досье — один рынок (нормализация
  имён стран через ads.geo), города таргетинга проходят как есть (доверенный источник).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bot.callbacks import ClientCB  # noqa: E402
from bot.keyboards import client_card_kb  # noqa: E402
from core.texts import fmt_client_card  # noqa: E402

CID = "7753643025"


def _profile(**over) -> dict:
    p = {
        "brand": "Kasi Motors",
        "business_desc": "автодилер, поддержанные авто, Найроби",
        "geo": "Кения",
        "language": "English",
        "website": "kasimotors.co.ke",
        "socials": {"instagram": "@kasimotors"},
        "notes": "упор на гарантию",
        "contacts": [{"kind": "phone", "value": "+254 712 345 678"}],
        "services": [
            {"name": "Седаны", "price": "от $4000", "category": "авто"},
            {"name": "Внедорожники", "price": "от $7000", "category": "авто"},
        ],
        "site_pages_count": 12,
        "last_crawled_at": "2026-07-16 10:00",
    }
    p.update(over)
    return p


# ── (а) карточка: секции, услуги списком, клип длинных тел, подсказка про досье ─────────
def test_card_sections_and_services_one_per_line():
    out = fmt_client_card(_profile(), CID)
    assert "Kasi Motors" in out and CID in out
    assert "<b>О бизнесе</b>" in out
    assert "<b>Услуги</b>" in out
    assert "• Седаны — от $4000 [авто]" in out  # по одной на строку, не «; »-простыня
    assert "• Внедорожники — от $7000 [авто]" in out
    assert "<b>Контакты</b>" in out and "+254 712 345 678" in out
    assert "Сайт: kasimotors.co.ke" in out
    assert "<b>Заметки</b>" in out
    assert "страниц с сайта: 12" in out and "2026-07-16 10:00" in out


def test_card_clips_long_body_hint_only_with_dossier():
    long = "х" * 400
    # обрезано, досье НЕТ → подсказки нет (кнопки «Досье» не будет — некуда вести)
    out = fmt_client_card(_profile(business_desc=long), CID)
    assert "…" in out and long not in out
    assert "Досье" not in out
    # обрезано, досье ЕСТЬ → подсказка
    out2 = fmt_client_card(_profile(business_desc=long), CID, has_dossier=True)
    assert "Полный текст — в 📄 Досье" in out2
    # ничего не обрезано → подсказки нет даже с досье
    out3 = fmt_client_card(_profile(), CID, has_dossier=True)
    assert "Досье" not in out3


def test_card_escapes_html_from_crawled_values():
    """Профиль собран из краула — значения извне, esc() обязателен (правило: файл извне)."""
    out = fmt_client_card(
        _profile(
            business_desc="<script>alert(1)</script>",
            services=[{"name": "<b>Седаны</b>", "price": None, "category": None}],
        ),
        CID,
    )
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>Седаны</b>" not in out and "&lt;b&gt;Седаны&lt;/b&gt;" in out


def test_card_empty_profile_and_en():
    out = fmt_client_card(None, CID)
    assert "Профиль пуст" in out
    out_en = fmt_client_card(_profile(), CID, "en")
    assert "<b>Services</b>" in out_en and "<b>Business</b>" in out_en


def test_card_many_services_counter():
    services = [{"name": f"Услуга {i}", "price": None, "category": None} for i in range(14)]
    out = fmt_client_card(_profile(services=services), CID)
    assert "• Услуга 9" in out and "• Услуга 10" not in out  # кап 10 как раньше
    assert "… и ещё 4" in out  # но менеджер видит, что услуг больше


# ── (г) клавиатура: callback_data нетронуты, изменились только ряды ─────────────────────
def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_card_kb_callbacks_preserved_and_rows_grouped():
    m = client_card_kb(True, True, customer_id=CID, has_dossier=True)
    datas = [b.callback_data for b in _flat(m)]
    assert datas == [
        ClientCB(action="autofill", sub=CID).pack(),
        ClientCB(action="dossier", sub=CID).pack(),
        ClientCB(action="update", sub=CID).pack(),
        ClientCB(action="recrawl").pack(),
        ClientCB(action="recrawl", sub="incr").pack(),
        ClientCB(action="clear").pack(),
        ClientCB(action="back").pack(),
    ]
    sizes = [len(row) for row in m.inline_keyboard]
    assert sizes == [2, 1, 2, 2]  # сгруппировано, не простыня по 1
    assert max(sizes) <= 2  # ряды не шире 2 — подписи кнопок длинные


def test_card_kb_no_profile_keeps_add_flow():
    m = client_card_kb(False, customer_id=CID)
    datas = [b.callback_data for b in _flat(m)]
    assert datas == [
        ClientCB(action="autofill", sub=CID).pack(),
        ClientCB(action="add", sub=CID).pack(),
        ClientCB(action="back").pack(),
    ]


def test_card_kb_profile_without_site_offers_crawl_path():
    m = client_card_kb(True, False, customer_id=CID)
    datas = [b.callback_data for b in _flat(m)]
    assert ClientCB(action="add", sub=CID).pack() in datas  # «Добавить сайт и краулить»
    assert ClientCB(action="recrawl").pack() not in datas


# ── (в) факты из аккаунта: гео из GAQL проходит КАК ЕСТЬ (гард от омонимов стран) ───────
async def test_account_facts_geo_passthrough_no_country_homonym_rewrite(monkeypatch):
    """GAQL-имя локации НЕ прогоняется через ads.geo: у имён есть омонимы стран — штат «Georgia»
    стал бы «Грузией» и уехал в гео-таргетинг §19. Источник доверенный (geo constants), гейт
    стран стоит только на краулёном тексте (clients.dossier.dossier_patch)."""
    import clients.account_facts as af

    async def fake_client(cid):
        return object()

    async def fake_run(fn, *args, label=""):
        if fn is af.list_campaigns:
            return [{"id": 1, "name": "C", "status": "ENABLED"}]
        if fn is af.read_campaign_targeting:
            # Georgia — ШТАТ США в таргетинге; Kampala — город: оба должны пройти нетронутыми
            return SimpleNamespace(locations=["Georgia", "Kampala"], languages=[])
        if fn is af.read_campaign_config:
            return None
        if fn is af.count_active_campaigns:
            return None
        return ""  # currency/timezone — не про этот тест

    monkeypatch.setattr(af, "build_client_async", fake_client)
    monkeypatch.setattr(af, "discovered_read_children_meta", lambda: {})
    monkeypatch.setattr(af, "run_ads_read_call", fake_run)

    patch = await af.fetch_account_facts("1234567890")
    assert patch["geo"] == "Georgia, Kampala"  # не «Грузия»: имена из GAQL не переписываются
