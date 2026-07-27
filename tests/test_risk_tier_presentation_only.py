"""Волна 5: тир риска — ПРЕЗЕНТАЦИОННЫЙ, и это свойство держится тестом, а не обещанием.

Исходная формулировка требования была «L1 — применять без человека». Реализовано иначе: тир решает
ФОРМУ вопроса (полнота карточки, вложение с графиком, число человеческих актов, срок жизни
согласия), а не нужен ли вопрос. Разница не косметическая — классификатор может ошибиться, и при
этом контракте ошибка стоит лишнего клика, а при «L1 = авто» стоила бы чужих денег.

Главный гард файла — AST: **ни один модуль `ads/**` не импортирует `confirm.risk`**. Пока это
верно, тир физически не может попасть ни в `ensure_allowed`, ни в `claim`, ни в провенанс, ни в
одну из девяти проверок §2.2 — не по дисциплине вызывающего, а потому что имени там нет.

Второй по важности — монотонность L1 ⊂ L2 ⊂ L3 и то, что неизвестность (нет снимка «было»)
классифицируется как L3. Отсутствие сведений — не доказательство безопасности.
"""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from confirm import render
from confirm.attachment import plan_attachment, plan_budget_chart
from confirm.consequences import MONTH_DAYS, PROJECTION_DAYS, consequences, projection_rows
from confirm.risk import (
    DESTRUCTIVE_OPS,
    L2_DELTA_PCT,
    L3_DELTA_PCT,
    MONEY_OPS,
    TIER_L1,
    TIER_L2,
    TIER_L3,
    TIERS,
    max_delta_pct,
    risk_tier,
)
from confirm.store import ConfirmStore, effective_ttl_hours
from core.config import settings

ROOT = Path(__file__).resolve().parents[1]
DRAFT = "7753643025"


def _budget_before(before: int, after: int, **extra) -> dict:
    """Снимок `_before`, который кладёт `ads/service.py::read_state` для update_budget."""
    return {
        "_before": {
            "kind": "budget",
            "before_micros": before,
            "after_micros": after,
            "currency": "USD",
            **extra,
        },
        "campaign": "Кампания",
        "mode": "set",
        "value": after / 1_000_000,
    }


# ─────────────────────────── граница: тир не доходит до исполнения ───────────────────────────


def test_ads_layer_never_imports_risk():
    """AST-гард контракта: `ads/**` не знает имени `confirm.risk` вовсе.

    Разбором исходников, а не зондом по `sys.modules`: ленивый `from confirm.risk import …` внутри
    функции зонд по модулям не увидит в принципе (проверено отрицательным контролем на C4). Проверка
    именно такая, потому что защищаемое свойство — не «сегодня не влияет», а «влиять неоткуда»."""
    bad: list[str] = []
    for path in sorted((ROOT / "ads").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                if n == "confirm.risk" or n.startswith("confirm.risk."):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not bad, f"тир риска протёк в слой исполнения: {bad}"


def test_risk_is_pure_no_db_no_network():
    """Классификатор — чистая функция: ни БД, ни сети, ни времени, ни `ads/**`.

    Чистота требуется двумя вызывающими: тир считает тул-слой в момент создания черновика и
    ПЕРЕСЧИТЫВАЕТ курьер вложений через минуту из персистентной строки. Совпасть они обязаны."""
    tree = ast.parse((ROOT / "confirm" / "risk.py").read_text(encoding="utf-8"))
    banned = ("ads", "db", "sqlalchemy", "aiogram", "httpx", "openai", "datetime", "confirm.store")
    bad: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for n in names:
            if any(n == b or n.startswith(b + ".") for b in banned):
                bad.append(f"{n} (строка {node.lineno})")
    assert not bad, f"классификатор перестал быть чистым: {bad}"


def test_money_ops_mirror_ads_resolve():
    """Зеркало `ads.resolve.MONEY_OPS` не разъехалось.

    Импортом набор не берётся намеренно (`confirm/**` не тянет `ads/**` ради трёх строк), поэтому
    дрейф ловится здесь: добавили денежную операцию в резолвер и забыли в классификаторе — она
    молча уехала бы в L1 и получила бы карточку без блока последствий."""
    from ads.resolve import MONEY_OPS as ADS_MONEY_OPS

    assert MONEY_OPS == frozenset(ADS_MONEY_OPS), (
        f"наборы денежных операций разошлись: {MONEY_OPS ^ frozenset(ADS_MONEY_OPS)}"
    )


def test_destructive_ops_are_the_single_source():
    """Литерала необратимых удалений в кнопочном слое больше нет — только импорт из `confirm.risk`.

    Раньше набор жил в `bot/main.py`, и второй потребитель (классификатор) неизбежно завёл бы свою
    копию: два списка на один вопрос — тот же класс дефекта, что обещание вложения без файла."""
    src = (ROOT / "bot" / "main.py").read_text(encoding="utf-8")
    assert "_DESTRUCTIVE_OPS = frozenset" not in src, "в bot/main.py снова свой литерал удалений"
    assert '"remove_asset_link"' not in src, "имена операций удаления снова перечислены в bot/main"


# ─────────────────────────── классификация ───────────────────────────


@pytest.mark.parametrize("op", sorted(DESTRUCTIVE_OPS))
def test_destructive_is_always_l3(op: str):
    """Необратимое удаление — L3 всегда, вне зависимости от params: повторный вызов ошибку не
    отменяет, поэтому размер здесь не смягчающее обстоятельство."""
    assert risk_tier(op, {}) == TIER_L3
    assert risk_tier(op, None) == TIER_L3


@pytest.mark.parametrize(
    "before,after,expect",
    [
        (100_000_000, 100_000_000, TIER_L1),  # без изменения
        (100_000_000, 104_000_000, TIER_L1),  # +4% < L2
        (100_000_000, 105_000_000, TIER_L2),  # ровно порог L2 — включительно
        (100_000_000, 140_000_000, TIER_L2),  # +40% < L3
        (100_000_000, 150_000_000, TIER_L3),  # ровно порог L3 — включительно
        (100_000_000, 300_000_000, TIER_L3),
        (100_000_000, 40_000_000, TIER_L3),  # −60%: снижение тоже бывает дорогим
        (0, 50_000_000, TIER_L3),  # рост из нуля: конечного процента нет
    ],
)
def test_money_tier_by_relative_delta(before: int, after: int, expect: str):
    """Пороги — ТОЛЬКО относительные. Абсолютного порога нет намеренно: суммы приходят в микро
    валюты аккаунта, и «100 000 000 микро» — это 100 USD и 100 UAH одновременно."""
    assert risk_tier("update_budget", _budget_before(before, after)) == expect


@pytest.mark.parametrize(
    "params",
    [
        {},  # снимка нет вовсе
        {"_before": None},
        {"_before": {"kind": "budget"}},  # снимок есть, чисел нет
        {"_before": {"kind": "budget", "before_micros": None, "after_micros": 1}},
        {"_before": "не словарь"},
        None,
    ],
)
def test_unknown_state_is_l3(params):
    """Нет снимка «было» — L3, а не L1. Отсутствие сведений о размере изменения не есть
    доказательство его малости; fail-closed здесь означает более полный показ, не отказ."""
    assert risk_tier("update_budget", params) == TIER_L3


def test_shared_budget_is_l3_regardless_of_size():
    """Общий бюджет — L3 даже при микроскопической дельте: изменение задевает ЧУЖИЕ кампании,
    которых нет в тексте команды, и радиус важнее процента."""
    p = _budget_before(100_000_000, 100_100_000, shared=True, shared_campaigns=["Другая"])
    assert risk_tier("update_budget", p) == TIER_L3
    assert risk_tier("update_budget", _budget_before(100_000_000, 100_100_000)) == TIER_L1


@pytest.mark.parametrize(
    "op", ["create_search_campaign", "create_gdn_campaign", "create_video_campaign"]
)
def test_create_ops_are_l2(op: str):
    """Создание тратящей сущности: обязательства есть, снимка «было» нет — сравнивать не с чем.
    Не L3 (двух актов за каждую новую кампанию никто не просил), но и не L1."""
    assert risk_tier(op, {}) == TIER_L2


def test_bid_list_takes_the_worst_group():
    """У списочных ставок берётся МАКСИМУМ, а не среднее: «в пяти группах +3%, в шестой +200%» —
    это риск шестой группы, и усреднение его прячет."""
    p = {
        "_before": {
            "kind": "bid",
            "before_micros": [1_000_000] * 6,
            "after_micros": [1_030_000] * 5 + [3_000_000],
            "currency": "USD",
        }
    }
    assert risk_tier("update_bid", p) == TIER_L3


def test_max_delta_pct_none_when_nothing_to_compare():
    assert max_delta_pct(None, None) is None
    assert max_delta_pct([], []) is None
    assert max_delta_pct("мусор", "мусор") is None
    assert max_delta_pct(0, 1) == float("inf")


def test_tiers_are_the_closed_set():
    """Классификатор не изобретает четвёртый тир: любой вход даёт значение из TIERS."""
    for op in ["update_budget", "remove_ad", "create_search_campaign", "pause_campaign", ""]:
        assert risk_tier(op, {}) in TIERS


def test_thresholds_are_ordered():
    """Пороги упорядочены — иначе ветка L2 недостижима, и монотонность L1 ⊂ L2 ⊂ L3 ломается."""
    assert 0 < L2_DELTA_PCT < L3_DELTA_PCT


# ─────────────────────────── последствия: числа из ОДНОГО снимка ───────────────────────────


def test_consequences_numbers_come_from_the_snapshot():
    """Каждое число блока выведено из тех же двух чисел, что напечатало «было → станет».

    Это и есть инвариант «один снимок»: второго источника чисел на денежной карточке нет, поэтому
    два разных «станет» на одной карточке невозможны по построению."""
    cons = consequences("update_budget", _budget_before(40_000_000, 48_000_000))
    assert cons is not None
    assert cons.before_micros == 40_000_000 and cons.after_micros == 48_000_000
    assert cons.delta_micros == 8_000_000
    assert cons.delta_pct == pytest.approx(20.0)
    assert cons.delta_horizon_micros == 8_000_000 * PROJECTION_DAYS
    assert cons.month_before_micros == int(40_000_000 * MONTH_DAYS)
    assert cons.month_after_micros == int(48_000_000 * MONTH_DAYS)
    # Прежний месячный потолок при новой скорости: 30.4 × 40/48 ≈ 25.3 дня.
    assert cons.days_to_month == pytest.approx(MONTH_DAYS * 40 / 48)
    assert cons.currency == "USD"


def test_consequences_days_to_month_only_on_increase():
    """Фраза «кончится за N дней вместо 30» считается только при РОСТЕ: при снижении она формально
    верна (дней больше 30), но отвечает на незаданный вопрос и читается как предупреждение."""
    down = consequences("update_budget", _budget_before(48_000_000, 40_000_000))
    assert down is not None and down.days_to_month is None
    assert down.delta_micros == -8_000_000


@pytest.mark.parametrize(
    "op,params",
    [
        ("update_bid", _budget_before(1_000_000, 2_000_000)),  # ставка ≠ скорость расхода
        ("update_keyword_bid", _budget_before(1_000_000, 2_000_000)),
        ("remove_campaign", {}),
        ("update_budget", {}),  # снимка нет
        ("update_budget", {"_before": {"kind": "bid", "before_micros": 1, "after_micros": 2}}),
        ("update_budget", {"_before": {"kind": "budget", "before_micros": -1, "after_micros": 2}}),
        ("update_budget", None),
    ],
)
def test_consequences_returns_none(op: str, params):
    """Отказ считать — молчаливый None, не исключение и не выдуманное число.

    Отдельно про ставки: перевести «+20% к ставке» в «+N в сутки» без модели аукциона нельзя, а
    выдуманное число на денежной карточке хуже отсутствующего."""
    assert consequences(op, params) is None


def test_projection_matches_the_text():
    """Расхождение линий на последнем дне графика РАВНО `delta_horizon_micros` из текста карточки:
    график и текст считаются из одних чисел и разойтись не могут."""
    cons = consequences("update_budget", _budget_before(40_000_000, 48_000_000))
    rows = projection_rows(cons, PROJECTION_DAYS)
    assert len(rows) == PROJECTION_DAYS and rows[0][0] == 1
    day, before_cum, after_cum = rows[-1]
    assert day == PROJECTION_DAYS
    assert after_cum - before_cum == cons.delta_horizon_micros


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_fmt_consequences_says_projection_not_simulation(lang: str):
    """Слово «симуляция» в тексте пользователю запрещено: оно обещает модель поведения, которой нет.
    Это линейная экстраполяция, и называется она так."""
    cons = consequences("update_budget", _budget_before(40_000_000, 48_000_000))
    out = render.fmt_consequences(cons, lang)
    assert out, "блок последствий пуст на L3-бюджете"
    low = out.lower()
    for banned in ("симуляц", "simulat", "прогноз расхода", "forecast of spend"):
        assert banned not in low, f"текст обещает симуляцию: {banned!r}"
    assert ("проекция" in low) or ("projection" in low)
    assert str(MONTH_DAYS) in out, "правило Google ×30.4 не названо"


def test_fmt_consequences_empty_on_none():
    assert render.fmt_consequences(None, "ru") == ""


def test_fmt_consequences_shows_minus_sign_on_decrease():
    """Снижение показано знаком минус (U+2212), а не «+»: направление на денежной карточке обязано
    быть видно с первого взгляда."""
    cons = consequences("update_budget", _budget_before(48_000_000, 40_000_000))
    out = render.fmt_consequences(cons, "ru")
    assert "−" in out and "+" not in out


# ─────────────────────────── вложение с графиком ───────────────────────────


def test_chart_only_for_l3_budget():
    """График — только у L3-бюджета. На L1/L2 он был бы шумом, на ставках — выдумкой."""
    assert (
        plan_budget_chart("update_budget", _budget_before(40_000_000, 100_000_000), cid="a")
        is not None
    )
    assert (
        plan_budget_chart("update_budget", _budget_before(100_000_000, 104_000_000), cid="a")
        is None
    )
    assert plan_budget_chart("update_bid", _budget_before(1_000_000, 5_000_000), cid="a") is None
    assert plan_budget_chart("remove_campaign", {}, cid="a") is None
    assert plan_budget_chart("update_budget", None, cid="a") is None


def test_chart_and_keyword_policies_never_collide():
    """Две политики вложения делят один флаг `attachment_state='pending'` — значит пересекаться они
    не должны. График живёт только на `update_budget`, которого нет в наборе ключевых операций."""
    from confirm.attachment import KEYWORD_XLSX_OPS

    assert "update_budget" not in KEYWORD_XLSX_OPS
    big = {"keywords": [f"к{i}" for i in range(render.KW_INLINE_MAX + 5)], "match_type": "EXACT"}
    for op in sorted(KEYWORD_XLSX_OPS):
        assert plan_budget_chart(op, big, cid="a") is None, f"{op} претендует на обе политики"


def test_chart_filenames_are_safe_and_unique():
    a = plan_budget_chart("update_budget", _budget_before(40_000_000, 100_000_000), cid="../../etc")
    b = plan_budget_chart("update_budget", _budget_before(40_000_000, 100_000_000), cid="bbbbbbbb")
    assert a and b and a.filename != b.filename
    assert "/" not in a.filename and "\\" not in a.filename


def test_projection_workbook_builds_and_saves(tmp_path):
    """Книга с графиком реально собирается и сохраняется openpyxl.

    Тест ровно про API диаграммы (LineChart/Reference): «рассуждением» его не проверить — ошибка в
    диапазоне даёт либо исключение при save, либо пустой график у клиента."""
    from reports.xlsx import build_budget_projection_workbook, write_budget_projection_xlsx

    spec = plan_budget_chart(
        "update_budget", _budget_before(40_000_000, 100_000_000), cid="cid123456789"
    )
    wb = build_budget_projection_workbook(spec, "ru")
    ws = wb.active
    # шапка + дисклеймер + пустая + заголовки + PROJECTION_DAYS строк данных
    assert ws.max_row == PROJECTION_DAYS + 4
    assert len(ws._charts) == 1, "график не попал на лист"
    texts = [str(c.value or "") for row in ws.iter_rows() for c in row]
    assert any("не прогноз аукциона" in t for t in texts), "лист не назвал проекцию проекцией"
    # Последний день: накопленный расход = дневной бюджет × 30 (в единицах, не в микро).
    assert ws.cell(row=ws.max_row, column=2).value == pytest.approx(40 * PROJECTION_DAYS)
    assert ws.cell(row=ws.max_row, column=3).value == pytest.approx(100 * PROJECTION_DAYS)
    out = write_budget_projection_xlsx(spec, str(tmp_path / "p.xlsx"), "ru")
    assert Path(out).stat().st_size > 0


def test_projection_workbook_discloses_shared_budget():
    """Общий бюджет: лист говорит, что проекция описывает не одну кампанию. Иначе он отвечал бы на
    вопрос про кампанию, а рисовал бы про чужие деньги."""
    from reports.xlsx import build_budget_projection_workbook

    p = _budget_before(40_000_000, 100_000_000, shared=True, shared_campaigns=["A", "B"])
    ws = build_budget_projection_workbook(
        plan_budget_chart("update_budget", p, cid="x"), "ru"
    ).active
    texts = [str(c.value or "") for row in ws.iter_rows() for c in row]
    assert any("Общий бюджет" in t for t in texts)


def test_courier_builds_both_kinds(tmp_path):
    """Курьер планировщика ветвится по `spec.kind` и собирает ОБА вида файла.

    Без этой ветки `BudgetChartSpec` уходил бы в сборщик списка ключей и падал на `spec.keywords` —
    строка осталась бы в 'pending' навсегда, а человек ждал бы обещанный график."""
    from scheduler.jobs import _build_attachment_file

    chart = plan_budget_chart(
        "update_budget", _budget_before(40_000_000, 100_000_000), cid="cid123456789"
    )
    p1 = _build_attachment_file(chart, "ru")
    assert Path(p1).stat().st_size > 0
    Path(p1).unlink()

    kw = plan_attachment(
        "add_keywords",
        {"keywords": [f"к{i}" for i in range(render.KW_INLINE_MAX + 5)], "match_type": "EXACT"},
        cid="cid123456789",
        lang="ru",
    )
    p2 = _build_attachment_file(kw, "ru")
    assert Path(p2).stat().st_size > 0
    Path(p2).unlink()


# ─────────────────────────── TTL согласия ───────────────────────────


def test_effective_ttl_is_monotone():
    """L3-настройка БОЛЬШЕ общего срока жизнь не удлиняет: `min`, а не выбор ветки.

    Смысл именно в этом — «ослабить TTL, подкрутив тир» должно быть невозможно, а не не принято."""
    assert effective_ttl_hours(None) == settings.proposal_ttl_hours
    assert effective_ttl_hours(TIER_L1) == settings.proposal_ttl_hours
    assert effective_ttl_hours(TIER_L3) <= settings.proposal_ttl_hours
    old = settings.proposal_ttl_hours_l3
    try:
        settings.proposal_ttl_hours_l3 = settings.proposal_ttl_hours + 100
        assert effective_ttl_hours(TIER_L3) == settings.proposal_ttl_hours, (
            "тир удлинил срок жизни согласия"
        )
    finally:
        settings.proposal_ttl_hours_l3 = old


def test_ttl_conjunct_is_additive_not_replacement():
    """Общее TTL-условие осталось в тех же CAS-запросах, а L3 добавлен КОНЪЮНКТОМ.

    Текстовый мета-гард (как в `tests/test_confirm_ttl_cas.py`): замена `_ttl_boundary()` на
    «умное» условие открыла бы возможность удлинить срок правкой одного места."""
    src = (ROOT / "confirm" / "store.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for name in ("claim", "confirm", "confirm_by_reply"):
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        )
        body = ast.get_source_segment(src, fn) or ""
        assert "_ttl_boundary()" in body, f"{name}: общее TTL-условие исчезло из CAS"
        assert "_l3_fresh()" in body, f"{name}: L3-конъюнкт не в CAS"
    reject = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "reject"
    )
    assert "_ttl_boundary()" not in (ast.get_source_segment(src, reject) or ""), (
        "отказ по TTL не ограничивают: протухший черновик обязан оставаться отклоняемым"
    )


@pytest.mark.asyncio
async def test_l3_consent_expires_earlier_than_general():
    """Живьём: L3-черновик возрастом между L3-сроком и общим — подтвердить НЕЛЬЗЯ, а такой же
    не-L3 — можно. Проверяется хранилищем (CAS), а не кодом между выборкой и записью."""
    from sqlalchemy import update

    from db.models import Proposal
    from db.session import Session, init_db

    await init_db()
    store = ConfirmStore()
    age_h = (settings.proposal_ttl_hours + settings.proposal_ttl_hours_l3) // 2
    assert settings.proposal_ttl_hours_l3 < age_h < settings.proposal_ttl_hours, (
        "настройки не дают окна между двумя сроками — тест бессмыслен"
    )
    made: dict[str, str] = {}
    for tier in (TIER_L1, TIER_L3):
        cid = uuid.uuid4().hex
        made[tier] = cid
        await store.save_proposal(
            confirmation_id=cid,
            operation="update_budget",
            customer_id=DRAFT,
            params=_budget_before(40_000_000, 48_000_000),
            summary="s",
            chat_id=777,
            user_initiated=True,
            risk_tier=tier,
        )
    async with Session() as s:
        await s.execute(
            update(Proposal)
            .where(Proposal.confirmation_id.in_(list(made.values())))
            .values(created_at=datetime.now(timezone.utc) - timedelta(hours=age_h))
        )
        await s.commit()
    assert await store.confirm(made[TIER_L1], chat_id=777) is True, "не-L3 умер раньше срока"
    assert await store.confirm(made[TIER_L3], chat_id=777) is False, (
        "L3-согласие пережило свой укороченный срок"
    )


@pytest.mark.asyncio
async def test_risk_tier_persisted_for_audit():
    """Тир попадает в строку черновика: аудит обязан отвечать на вопрос «что человек видел, когда
    соглашался». Пересчёт задним числом ответит про сегодняшние пороги, а не про действовавшие."""
    from sqlalchemy import select

    from db.models import Proposal
    from db.session import Session, init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    params = _budget_before(40_000_000, 100_000_000)
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT,
        params=params,
        summary="s",
        chat_id=1,
        user_initiated=True,
        risk_tier=risk_tier("update_budget", params),
    )
    async with Session() as s:
        row = (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()
    assert row.risk_tier == TIER_L3
