"""Ревизия волны: гарды честности карточки аудита. Каждый тест закрывает КЛАСС бага, а не случай.

Общий корень всех шести: «чек ослеп» и «у клиента всё хорошо» — это РАЗНЫЕ утверждения (GR8), но код
их путал, и молчание слепого чека читалось клиентом как «проблем нет»:

1. Семья говорила «✅ Проверено, проблем нет», когда её сигнал не прочитан (у `waste` записи в
   `_FAMILY_SIGNALS` не было вовсе, у `rsa` не было самих объявлений) — карта теперь выводится ИЗ
   КОДА чеков (AST, транзитивно через хелперы), дрейф падает тестом.
2. «⚡ Быстрые победы (в один тап)» показывались из ВСЕХ находок, а кнопки бот рисует только первым
   `_AUDIT_MAX_FINDINGS` — обещание «в один тап» без тапа.
3. Семьи `competition`/`recommendations` не имели меток — в карточку лез сырой идентификатор.
4. Усечённый инвентарь ключей (упор в LIMIT) — это НЕ «столько у меня ключей»: майнинг использует его
   как отрицательный фильтр, и на неполном списке советует собрать уже собранное / заминусовать своё
   же слово (применить = убить трафик). Такие чеки обязаны молчать.
5. PMax-кампании не прочитаны (None) → «нет фида» — ложное обвинение ритейлу.
6. RSA прочитаны, но даты старта нет ни у одной (`start_date_time` — ограничение расписания, а не
   дата создания) ⇒ `rsa_stale` слеп ⇒ collect обязан объявить пробел `rsa_age`.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.queries import Breakdown, KeywordInventoryRow, Metrics  # noqa: E402

from audit import render  # noqa: E402
from audit.engine import CHECK_REGISTRY, build_audit  # noqa: E402

_ENGINE = Path(__file__).resolve().parents[1] / "audit" / "engine.py"

# ctx-поле _Ctx → имя сигнала в data_gaps (то, что collect-слой реально объявляет пробелом).
# Пусто = поле НЕ сигнал чтения: `target_for`/`brand_terms` (локальные), симуляторы (их отсутствие —
# норма, Google строит их не всегда), `keyword_inventory_truncated` (флаг, а не данные).
_CTX_TO_SIGNAL = {
    "is_rows": "impression_share",
    "conversion_actions": "conversion_actions",
    "bidding_by_name": "bidding",
    "search_terms": "search_terms",
    "recommendations": "recommendations",
    "ad_policy": "ad_policy",
    "bid_landscape": "bid_landscape",
    "adgroup_structure": "adgroup_structure",
    "negative_lists": "negative_lists",
    "keyword_quality": "keyword_quality",
    "geo_waste": "geo_waste",
    "schedule": "schedule",
    "rsa_ads": "rsa_ads",
    "keyword_inventory": "keyword_inventory",
    "campaign_assets": "campaign_assets",
    "campaign_settings": "campaign_settings",
    "pmax_campaigns": "pmax",
    "pmax_asset_groups": "pmax",
    "target_for": None,
    "brand_terms": None,
    "bid_simulations": None,
    "budget_simulations": None,
    "keyword_inventory_truncated": None,
}


def _ctx_reads_by_family() -> dict[str, set[str]]:
    """Какие ctx-сигналы читает КАЖДАЯ семья — выведено из кода чеков (AST), а не переписано руками.
    Обход транзитивный: половина чеков ходит в ctx через хелперы (`_serving_rsa`, `_pmax_rows`, …)."""
    src = _ENGINE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def attrs(name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in funcs:
            return set()
        seen.add(name)
        out: set[str] = set()
        for n in ast.walk(funcs[name]):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "ctx"
            ):
                out.add(n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                out |= attrs(n.func.id, seen)
        return out

    fam_of = {cid: fam for cid, (fam, _sev) in CHECK_REGISTRY.items()}
    by_family: dict[str, set[str]] = {}
    for name, node in funcs.items():
        if not name.startswith("check_"):
            continue
        body = ast.get_source_segment(src, node) or ""
        used = attrs(name, set())
        signals = {_CTX_TO_SIGNAL[a] for a in used if a in _CTX_TO_SIGNAL and _CTX_TO_SIGNAL[a]}
        for cid in re.findall(r'check_id="([a-z0-9_]+)"', body):
            fam = fam_of.get(cid)
            if fam:
                by_family.setdefault(fam, set()).update(signals)
    return by_family


def test_family_signals_cover_ctx_reads():
    """Гард дрейфа: если чек семьи читает сигнал, а сигнал не заявлен в `_FAMILY_SIGNALS`, семья
    скажет «✅ проблем нет» ровно там, где чек ослеп. Карта обязана ПОКРЫВАТЬ (⊇) чтения кода —
    надмножество разрешено (`rsa_age` — пробел, выводимый не из ctx, а из содержимого RSA-строк)."""
    reads = _ctx_reads_by_family()
    for family in render._IMPLEMENTED_FAMILIES:
        declared = set(render._FAMILY_SIGNALS.get(family, ()))
        missing = reads.get(family, set()) - declared
        assert not missing, (
            f"семья {family!r} читает {sorted(missing)}, но не объявила их в _FAMILY_SIGNALS: "
            f"при сбое этих чтений карточка соврёт «проблем нет»"
        )


def _collect_signal_vocabulary() -> set[str]:
    """Имена сигналов, которыми оперирует слой сбора (список пар в `data_gaps`, audit/collect.py) —
    выведены из кода (AST), а не переписаны руками. `pmax` добавляется явно: два чтения — один сигнал.
    `rsa_age`/усечение инвентаря сюда НЕ входят: это пробелы, ВЫВЕДЕННЫЕ из содержимого прочитанных
    строк, а не «чтение не удалось»."""
    src = (Path(__file__).resolve().parents[1] / "audit" / "collect.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "data_gaps" for t in node.targets)
            and isinstance(node.value, ast.ListComp)
        ):
            return {
                el.value
                for gen in node.value.generators
                for tup in ast.walk(gen.iter)
                if isinstance(tup, ast.Tuple) and tup.elts
                for el in tup.elts[:1]
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            } | {"pmax"}
    raise AssertionError("не найден список сигналов data_gaps в audit/collect.py")


def test_engine_ctx_signals_speak_the_same_vocabulary_as_collect():
    """Провенанс сбора (`AuditResult.ctx_signals`) и пробелы (`data_gaps`) обязаны называть сигналы
    ОДИНАКОВО: по первому потребители советов решают, какой семье верить (CTX_ONLY_FAMILIES), по
    второму карточка решает, где сказать «недостаточно данных». Разъедься имена — гейт молча
    перестанет срабатывать (сигнала с таким именем в ctx_signals просто нет ⇒ семья слепа НАВСЕГДА)
    или сработает вхолостую. Ловит «добавил ctx-сигнал в collect — забыл в build_audit»."""
    full = build_audit(
        _report(100.0, conv=0.0),
        is_rows=[],
        conversion_actions=[],
        bidding=[],
        optimization_score=SimpleNamespace(score=0.9, uplift=0.1),
        recommendations=[],
        search_terms=[],
        ad_policy=[],
        adgroup_structure=[],
        negative_lists=SimpleNamespace(count=0, campaign_level_count=0),  # объект, не список
        keyword_quality=[],
        geo_waste=[],
        schedule=[],
        bid_landscape=[],
        rsa_ads=[],
        keyword_inventory=[],
        campaign_assets=[],
        campaign_settings=[],
        pmax_campaigns=[],
        pmax_asset_groups=[],
    )
    assert set(full.ctx_signals) == _collect_signal_vocabulary()
    # Пустой список — ПОДАННЫЙ сигнал («прочитано, строк нет» ≠ «не прочитано»), как и в collect.
    assert build_audit(_report(100.0)).ctx_signals == frozenset()  # engine-only: ничего не подали


def test_ctx_only_families_declare_signals_their_checks_really_read():
    """Политика гейта (advisor.from_findings.CTX_ONLY_FAMILIES: семья → сигнал) обязана совпадать с
    КОДОМ чеков. Объявишь семье сигнал, которого её чеки не читают, — гейт будет молчать по семье,
    которая на самом деле не слепнет (потеря честных советов, тихая). Промахнёшься в имени — гейт не
    сработает вовсе (ложь дойдёт до клиента с кнопкой «применить»)."""
    from advisor.from_findings import CTX_ONLY_FAMILIES

    reads = _ctx_reads_by_family()
    vocab = _collect_signal_vocabulary()
    for family, signal in CTX_ONLY_FAMILIES.items():
        assert signal in vocab, f"{family!r}: сигнал {signal!r} не существует (опечатка?)"
        assert signal in reads.get(family, set()), (
            f"семья {family!r} объявлена зависимой от {signal!r}, но её чеки его не читают — "
            f"гейт душит советы, которые на самом деле честны"
        )


def test_every_signal_and_family_has_labels_in_both_languages():
    """Всё, что попадает в карточку, имеет человекочитаемую метку на RU и EN. Иначе клиенту
    показывают сырой идентификатор («adgroup_structure», «competition»)."""
    families = {fam for fam, _sev in CHECK_REGISTRY.values()}
    assert families  # реестр не пуст (иначе тест зелен вхолостую)
    for family in families:
        for lang in ("ru", "en"):
            assert family in render._FAMILY_LABEL[lang], f"нет метки семьи {family!r} ({lang})"
    for family in render._IMPLEMENTED_FAMILIES:
        for signal in render._FAMILY_SIGNALS.get(family, ()):
            for lang in ("ru", "en"):
                assert signal in render._SIGNAL_LABEL[lang], (
                    f"нет метки сигнала {signal!r} ({lang})"
                )


def test_quick_win_pool_matches_bot_slice():
    """«В один тап» можно обещать ТОЛЬКО находке, которой бот нарисует кнопку. Кнопки получают
    первые `_AUDIT_MAX_FINDINGS` находок — значит и пул быстрых побед ровно такой."""
    import bot.main as bm

    assert render.QUICK_WIN_POOL == bm._AUDIT_MAX_FINDINGS


def _report(cost: float, conv: float = 0.0, campaign: str = "Поиск"):
    m = Metrics(impressions=1000, clicks=100, cost_micros=int(cost * 1e6), conversions=conv)
    return SimpleNamespace(
        customer_id="123",
        totals=m,
        currency="USD",
        breakdowns=[
            Breakdown("campaign", "Кампании", ["Кампания", "Статус"], [((campaign, "ENABLED"), m)])
        ],
    )


def _st(term: str, cost: float, conv: float, campaign: str = "Поиск"):
    return SimpleNamespace(
        campaign=campaign,
        ad_group="Группа",
        search_term=term,
        metrics=Metrics(impressions=100, clicks=20, cost_micros=int(cost * 1e6), conversions=conv),
    )


def _kw(text: str, campaign: str = "Поиск"):
    return KeywordInventoryRow(
        campaign=campaign,
        ad_group="Группа",
        keyword=text,
        match_type="PHRASE",
        metrics=Metrics(impressions=10, clicks=1, cost_micros=1_000_000, conversions=0.0),
    )


def test_truncated_keyword_inventory_silences_mining_checks():
    """Инвентарь упёрся в LIMIT ⇒ он неполон. Майнинг (harvest / n-граммы) на неполном списке даёт
    ВРЕДНЫЙ совет: «собери» уже собранное, «заминусуй» своё же слово. Молчим, как при None."""
    rep = _report(1000.0, 10.0)
    terms = [_st("купить ремонт кухни", 200.0, 5.0), _st("ремонт кухни бесплатно", 150.0, 0.0)]
    inv = [_kw("ремонт ванной")]

    live = build_audit(rep, search_terms=terms, keyword_inventory=inv)
    mining = {"keyword_harvest", "ngram_waste"}
    assert mining & {f.check_id for f in live.findings}, (
        "предпосылка теста: на полном списке чеки ГОВОРЯТ"
    )

    cut = build_audit(
        rep, search_terms=terms, keyword_inventory=inv, keyword_inventory_truncated=True
    )
    assert not (mining & {f.check_id for f in cut.findings})


def test_keyword_inventory_readers_honor_truncation():
    """AST-инвариант (закрывает КЛАСС, не три случая): любой check_*, читающий ctx.keyword_inventory
    (прямо ИЛИ транзитивно через хелпер вроде `_kw_texts`), ОБЯЗАН считаться с
    ctx.keyword_inventory_truncated. Иначе выдаст находку по произвольному обрезку LIMIT'ом — доля
    нулевых, дубли-каннибалы и бренд-микс на неполном списке систематически врут (GR8)."""
    src = _ENGINE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def ctx_attrs(name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in funcs:
            return set()
        seen.add(name)
        out: set[str] = set()
        for n in ast.walk(funcs[name]):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "ctx"
            ):
                out.add(n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                out |= ctx_attrs(n.func.id, seen)
        return out

    offenders = [
        name
        for name in funcs
        if name.startswith("check_")
        and "keyword_inventory" in (used := ctx_attrs(name, set()))
        and "keyword_inventory_truncated" not in used
    ]
    assert not offenders, (
        f"чеки читают keyword_inventory без учёта усечения: {offenders} — "
        "добавь ранний выход `or ctx.keyword_inventory_truncated`"
    )


def test_truncated_keyword_inventory_silences_share_check():
    """Самый опасный из трёх (ложно-ПОЛОЖИТЕЛЬНЫЙ): zero_impression_keywords судит по ДОЛЕ нулевых.
    На обрезке LIMIT'ом (произвольное подмножество без ORDER BY) доля бессмысленна → чек молчит."""
    rep = _report(1000.0, 10.0)

    def _kw0(text: str):  # ключ без показов
        return KeywordInventoryRow(
            campaign="Поиск",
            ad_group="Группа",
            keyword=text,
            match_type="PHRASE",
            metrics=Metrics(impressions=0, clicks=0, cost_micros=0, conversions=0.0),
        )

    inv = [_kw0(f"нулевой ключ {i}") for i in range(25)]
    live = build_audit(rep, keyword_inventory=inv)
    assert "zero_impression_keywords" in {f.check_id for f in live.findings}, (
        "предпосылка теста: на полном списке чек ГОВОРИТ"
    )
    cut = build_audit(rep, keyword_inventory=inv, keyword_inventory_truncated=True)
    assert "zero_impression_keywords" not in {f.check_id for f in cut.findings}


def test_score_affecting_gaps_excludes_second_opinion_signals():
    """F: пробел «второго мнения» Google (opt_score/recommendations) балл НЕ двигает — тренд по нему
    честен и снапшот пишется. Пробел сигнала весомой семьи (конверсии/IS/инвентарь) балл ЗАВЫШАЕТ —
    тренд по нему обязан быть «н/д», а снапшот — не записан (иначе отравит baseline)."""
    from audit.render import score_affecting_gaps

    assert score_affecting_gaps(SimpleNamespace(data_gaps=None)) == set()
    assert (
        score_affecting_gaps(SimpleNamespace(data_gaps=["optimization_score", "recommendations"]))
        == set()
    )
    assert score_affecting_gaps(
        SimpleNamespace(data_gaps=["conversion_actions", "optimization_score"])
    ) == {"conversion_actions"}
    for sig in ("impression_share", "keyword_inventory", "bidding", "campaign_assets"):
        assert score_affecting_gaps(SimpleNamespace(data_gaps=[sig])) == {sig}


def test_card_flags_score_affecting_gap_as_overstated_not_harmless():
    """F (GR8): пробел весомой семьи в карточке — «балл может быть завышен», НЕ обманчивое «на score
    не влияет». Мягкая формулировка остаётся ТОЛЬКО для второго мнения Google (opt_score)."""
    from audit.render import render_audit

    rep = _report(500.0, 5.0)
    res = build_audit(rep, data_gaps=["conversion_actions", "optimization_score"])
    card = render_audit(res, "ru")
    assert "балл может быть завышен" in card, (
        "пробел весомой семьи обязан честно пометить завышение"
    )
    assert "на score не влияет" in card, "второе мнение Google остаётся мягкой пометкой"


async def _gaps(monkeypatch, *, rsa_rows, kw_rows) -> set[str]:
    """data_gaps, которые объявит collect-слой при таких RSA/инвентаре (остальные фетчеры — []),
    т.е. НЕ сбой чтения: список пуст → «данных нет», а не «сигнал потерян»."""
    from audit.collect import gather_audit

    fake_report = SimpleNamespace(customer_id="1", totals=Metrics(), breakdowns=[], currency="USD")

    async def fake_build_report(client, cid, period, **kw):
        return fake_report

    async def fake_run(fn, *args, label=""):
        if label == "audit_currency":
            return "USD"
        if label == "audit_rsa_assets":
            return rsa_rows
        if label == "audit_keyword_inventory":
            return kw_rows
        return []

    monkeypatch.setattr("reports.service.build_account_report_async", fake_build_report)
    monkeypatch.setattr("core.resilience.run_ads_read_call", fake_run)
    res = await gather_audit(object(), "1", None)
    return set(res.data_gaps or ())


async def test_collect_declares_rsa_age_gap_when_no_ad_has_a_start_date(monkeypatch):
    """`ad_group_ad.start_date_time` — ограничение расписания, а не дата создания: у обычного RSA оно
    ПУСТО ⇒ age_days=-1 ⇒ rsa_stale молчит всегда. Без пробела `rsa_age` карточка сказала бы, что с
    объявлениями всё хорошо, — а мы про их возраст просто ничего не знаем (GR8)."""
    ad = SimpleNamespace(age_days=-1)
    assert "rsa_age" in await _gaps(monkeypatch, rsa_rows=[ad], kw_rows=[])
    # Дата есть хотя бы у одного → чек зряч, пробела нет.
    assert "rsa_age" not in await _gaps(
        monkeypatch, rsa_rows=[ad, SimpleNamespace(age_days=120)], kw_rows=[]
    )
    # RSA нет вовсе → это не «пробел в возрасте», а «объявлений нет» (о них скажут другие чеки).
    assert "rsa_age" not in await _gaps(monkeypatch, rsa_rows=[], kw_rows=[])


async def test_collect_declares_gap_when_keyword_inventory_hits_the_limit(monkeypatch):
    """Инвентарь упёрся в LIMIT ⇒ он усечён ⇒ это пробел, а не «столько ключей». Иначе карточка
    молчит о том, что майнинг-чеки отключены, и клиент читает их молчание как «нечего собирать»."""
    from reports.queries import KEYWORD_INVENTORY_LIMIT

    full = [_kw(f"ключ {i}") for i in range(KEYWORD_INVENTORY_LIMIT)]
    assert "keyword_inventory" in await _gaps(monkeypatch, rsa_rows=[], kw_rows=full)
    assert "keyword_inventory" not in await _gaps(monkeypatch, rsa_rows=[], kw_rows=full[:-1])


def test_pmax_no_signals_stays_silent_when_campaigns_not_read():
    """PMax не прочитан (None) ≠ «фида нет». Обвинить ритейл в отсутствии Merchant-фида по факту
    сбоя чтения — ложное обвинение, а совет по нему бесполезен."""
    rep = _report(500.0, 5.0)
    assert "pmax_no_signals" not in {f.check_id for f in build_audit(rep).findings}
    assert "pmax_no_signals" not in {
        f.check_id for f in build_audit(rep, pmax_campaigns=None).findings
    }
