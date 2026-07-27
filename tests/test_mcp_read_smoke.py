"""Офлайн-smoke MCP READ-слоя (без SDK/БД/сети): конверт, сериализация, редакция, форвард kwargs.

SDK-путь подменён (monkeypatch `build_client_async`/`run_ads_read_call` в `mcp_server.tools_read`) —
как `mut._apply_*_via_sdk` в test_write_layer.py. Проверяем ровно наш слой поверх ридера:
пагинацию/усечение/`total_rows`, `code_numbers ⊇ числа rows`, метрики, считаемые КОДОМ, редакцию
ошибки (правило 5) и условный проброс kwargs в keyword_ideas. Живой Draft-прогон — отдельно.

Стиль — как tests/test_safety_core.py: `sys.path.insert` + `# noqa: E402`, in-process, `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reports.tz as rtz  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from mcp_server import envelope, serialize  # noqa: E402
from mcp_server import tools_read as tr  # noqa: E402
from mcp_server.redact import redact_error  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402


@contextmanager
def _read_allowed():
    """Замок ЧТЕНИЯ стоит на ГРАНИЦЕ слоя (`tools_read._guarded`), а не только внутри ридера, поэтому
    офлайн-смоук обязан звать инструмент на РАЗРЕШЁННОМ аккаунте. Иначе он молча проверял бы отказ
    замка вместо конверта — и остался бы зелёным при сломанной сериализации (сам замок покрыт
    параметризованным инвариантом в tests/test_hermes_isolation.py)."""
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = DRAFT_ACCOUNT_ID
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def _manager_allowed(mid: str):
    """Замок ОБХОДА MCC (`ensure_manager_allowed`) — иной чокпойнт, чем чтение: разрешает ТОЛЬКО
    настроенные MCC (`settings.login_customer_id_set`, вывод из `google_ads_login_customer_id`).
    Чтобы офлайн-смоук `list_accounts` проверял конверт, а не отказ замка, менеджерский id должен
    быть среди настроенных."""
    prev = settings.google_ads_login_customer_id
    settings.google_ads_login_customer_id = mid
    try:
        yield
    finally:
        settings.google_ads_login_customer_id = prev


def _sample_breakdown() -> Breakdown:
    return Breakdown(
        key="campaign",
        title="Кампании",
        dim_headers=["Кампания", "Статус"],
        rows=[
            (("Camp A", "ENABLED"), Metrics(100, 10, 5_000_000, 2.0, 20.0)),
            (("Camp B", "PAUSED"), Metrics(50, 5, 1_000_000, 1.0, 5.0)),
        ],
    )


# ── Конверт: пагинация / усечение / total_rows ──────────────────────────────────────
def test_envelope_paginate_signals():
    rows = [{"n": i} for i in range(5)]
    page, total, truncated = envelope.paginate(rows, offset=0, limit=2)
    assert total == 5 and len(page) == 2 and truncated is True
    page, total, truncated = envelope.paginate(rows, offset=4, limit=2)
    assert len(page) == 1 and truncated is False  # хвост: 4-я строка, дальше нет


def test_envelope_ok_shape_and_code_numbers():
    env = envelope.ok([{"cost": 5.0}, {"cost": 2.5}], offset=0, limit=1, extra={"title": "T"})
    assert env["total_rows"] == 2 and env["returned"] == 1 and env["truncated"] is True
    assert env["error"] is None and env["title"] == "T"
    # code_numbers покрывает и rows, и extra-числа; сериализуемо (list, не set).
    assert isinstance(env["code_numbers"], list)
    assert 5.0 in env["code_numbers"]


def test_code_numbers_never_exceed_what_the_model_sees():
    """Направление ⊆ (обратное строке выше, и оно — про безопасность, а не про полноту).

    `code_numbers` — allow-list ЦИТИРОВАНИЯ для `factguard.narrative_facts_preserved`. Он обязан
    быть ПОДМНОЖЕСТВОМ чисел, которые реально уехали в окно модели. Шире — и агент вправе
    процитировать число со страницы, которой не видел: «расход 2.5» пройдёт fact-guard как
    проверенное кодом, хотя это чистая галлюцинация.

    Сегодня свойство держится тем, что `ok()` считает `collect_numbers` по УЖЕ нарезанной странице.
    Тест существует затем, что любой слой поверх конверта (сжатие ответа перед подачей в контекст —
    ровно то, что предлагала ветка `main`) первым делом выбрасывает строки и переносит
    `code_numbers` как есть. Тогда allow-list становится шире видимого — молча.

    «Видимое» берём из САМОГО конверта минус `code_numbers` (иначе assert тавтологичен) — это то,
    что FastMCP сериализует в JSON и отдаёт модели."""
    from audit.factguard import collect_numbers

    env = envelope.ok([{"cost": 5.0}, {"cost": 2.5}], offset=0, limit=1, extra={"title": "T"})
    assert 2.5 not in env["code_numbers"], "число со ВТОРОЙ страницы не citeable"
    visible = collect_numbers({k: v for k, v in env.items() if k != "code_numbers"})
    assert set(env["code_numbers"]) <= visible


def test_envelope_err_skeleton_and_redaction():
    env = envelope.err(RuntimeError("boom: token=SUPERSECRETVALUE123"))
    assert env["rows"] == [] and env["total_rows"] == 0 and env["truncated"] is False
    assert env["code_numbers"] == []
    assert env["error"] and "SUPERSECRETVALUE123" not in env["error"]  # правило 5


# ── Сериализация: производные метрики считает КОД ───────────────────────────────────
def test_metrics_dict_computed_by_code():
    m = Metrics(100, 10, 5_000_000, 2.0, 20.0)
    d = serialize.metrics_dict(m)
    assert d["cost"] == 5.0  # 5_000_000 micros
    assert d["ctr"] == 0.1  # 10/100
    assert d["avg_cpc"] == 0.5  # 5.0/10
    assert d["cpa"] == 2.5  # 5.0/2
    assert d["roas"] == 4.0  # 20/5


def test_breakdown_rows_shape():
    rows = serialize.breakdown_rows(_sample_breakdown())
    assert rows[0]["dimensions"] == {"Кампания": "Camp A", "Статус": "ENABLED"}
    assert rows[0]["metrics"]["cost"] == 5.0


# ── Редакция ошибок наружу (без aiogram) ────────────────────────────────────────────
def test_redact_error_scrubs_secret_and_returns_str():
    # Синтетический refresh-token (форма '1//', не реальный); тест доказывает редакцию.
    secret = "1//0abcSECRETxyz"  # gitleaks:allow
    out = redact_error(RuntimeError(f"oauth failed: refresh_token={secret}"))
    assert isinstance(out, str)
    assert secret not in out and "Traceback" not in out


# ── Полный путь обёртки офлайн (SDK подменён) ───────────────────────────────────────
def test_get_campaign_stats_envelope_offline(monkeypatch):
    async def fake_client(customer_id=None):
        return object()

    async def fake_read(*args, **kwargs):
        return _sample_breakdown()

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", fake_read)

    with _read_allowed():
        env = asyncio.run(
            tr.get_campaign_stats(account=DRAFT_ACCOUNT_ID, period_days=7, limit=1, offset=0)
        )
    assert env["error"] is None and env["error_code"] is None
    assert env["total_rows"] == 2 and env["returned"] == 1 and env["truncated"] is True
    assert env["title"] == "Кампании"
    assert env["rows"][0]["dimensions"]["Кампания"] == "Camp A"
    assert 5.0 in env["code_numbers"]  # cost первой кампании — citeable-число


def test_wrapper_catches_reader_failure_into_envelope(monkeypatch):
    async def fake_client(customer_id=None):
        return object()

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream failed: api_key=SECRET_TOKEN_ABC")

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", boom)

    with _read_allowed():
        env = asyncio.run(tr.get_campaign_stats(account=DRAFT_ACCOUNT_ID))
    assert env["rows"] == [] and env["error"]  # fail-closed: сбой → error-конверт, не исключение
    assert "SECRET_TOKEN_ABC" not in env["error"]  # правило 5
    # Код доказывает, что поймали сбой РИДЕРА, а не отказ замка — иначе тест зелёный по другой причине.
    assert env["error_code"] == "internal"


def test_keyword_ideas_forwards_only_set_kwargs(monkeypatch):
    captured: dict = {}

    async def fake_client(customer_id=None):
        return object()

    async def capture(fn, *args, **kwargs):
        captured.update(kwargs)
        return []  # generate_keyword_ideas отдал бы list[KeywordIdea]; пустой список валиден

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", capture)

    with _read_allowed():
        asyncio.run(
            tr.keyword_ideas(
                account=DRAFT_ACCOUNT_ID, seeds=["a", "b"], geo_ids=[2840], reader_limit=10
            )
        )
    # заданные — проброшены (geo_ids → tuple, reader_limit → limit); незаданные (url/language/network) — нет
    assert captured["seeds"] == ["a", "b"]
    assert captured["geo_ids"] == (2840,)
    assert captured["limit"] == 10
    assert "url" not in captured and "language" not in captured and "network" not in captured
    # служебные метки run_ads_read_call (account/label) не в счёт — это не kwargs ридера
    assert captured.get("account") == DRAFT_ACCOUNT_ID


# ── §8: окно пере-якорено на TZ АККАУНТА, а не хоста ────────────────────────────────
def _capture_period(monkeypatch, tz_returns: date | None):
    """Общий каркас: фейк-клиент, ловим period, дошедший до ридера; account_today (источник даты
    аккаунта) фиксируем или помечаем вызов. Возвращает (captured, called)."""
    captured: dict = {}
    called = {"tz": False}

    async def fake_client(customer_id=None):
        return object()

    async def capture(fn, *args, **kwargs):
        captured["period"] = args[2]  # (client, account, period, campaign_id, ...)
        return _sample_breakdown()

    async def fake_account_today(client, customer_id, *, label="acct_tz"):
        called["tz"] = True
        return tz_returns

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", capture)
    monkeypatch.setattr(rtz, "account_today", fake_account_today)
    return captured, called


def test_relative_period_reanchored_to_account_today(monkeypatch):
    """Относительное окно («последние 7 дн.») строится от «сегодня» АККАУНТА. Без этого для аккаунта
    в чужой TZ (UG при прогоне из Европы) последний полный день выпадал из окна и цифры MCP
    расходились с интерфейсом Google Ads. account_today фиксирован 2020-06-15 (≠ host date.today())."""
    captured, called = _capture_period(monkeypatch, tz_returns=date(2020, 6, 15))
    with _read_allowed():
        asyncio.run(tr.get_campaign_stats(account=DRAFT_ACCOUNT_ID, period_days=7))
    p = captured["period"]
    # last 7 дней от 15 июня: [08 июня .. 14 июня] — «вчера» аккаунта, без неполного сегодня
    assert p.date_from == date(2020, 6, 8) and p.date_to == date(2020, 6, 14) and p.days == 7
    assert called["tz"] is True  # относительное окно ⇒ TZ аккаунта прочитана


def test_custom_period_not_reanchored_and_skips_tz_read(monkeypatch):
    """custom-диапазон (обе ISO-даты названы менеджером явно) account_period НЕ трогает, и TZ
    аккаунта не читает вовсе (незачем тратить запрос) — иначе явно заказанный «за январь 2019» уехал
    бы на день."""
    captured, called = _capture_period(monkeypatch, tz_returns=date(2020, 6, 15))
    with _read_allowed():
        asyncio.run(
            tr.get_campaign_stats(
                account=DRAFT_ACCOUNT_ID, date_from="2019-01-01", date_to="2019-01-31"
            )
        )
    p = captured["period"]
    assert p.date_from == date(2019, 1, 1) and p.date_to == date(2019, 1, 31)
    assert called["tz"] is False  # custom ⇒ TZ не читается


# ── reader_capped: GAQL-усечение ≠ пагинация конверта ───────────────────────────────
def test_envelope_reader_capped_when_total_reaches_reader_limit():
    """total достиг GAQL-LIMIT ридера ⇒ reader_capped=true + эхо reader_limit. Отдельно от truncated:
    полный набор БОЛЬШЕ total_rows и следующим offset НЕ добирается."""
    rows = [{"n": i} for i in range(200)]
    env = envelope.ok(rows, offset=0, limit=50, reader_limit=200)
    assert env["reader_capped"] is True and env["reader_limit"] == 200
    assert env["total_rows"] == 200 and env["truncated"] is True  # ещё и пагинация конверта


def test_envelope_not_reader_capped_below_limit():
    """total < reader_limit ⇒ ридер вернул всё, reader_capped=false, ключа reader_limit нет."""
    env = envelope.ok([{"n": i} for i in range(3)], offset=0, limit=50, reader_limit=200)
    assert env["reader_capped"] is False and "reader_limit" not in env


def test_get_search_terms_signals_reader_cap_offline(monkeypatch):
    """Кейс аудита: аккаунт с 5000 запросов, ридер тянет топ-N по расходу. get_search_terms не должен
    отдавать total_rows как полный счёт — reader_capped=true говорит модели «запросов ≥ N, не ровно
    N» (иначе минус-слова строятся на ложном «ровно N»)."""
    from reports.queries import Metrics, SearchTermRow

    n = 5  # ридер вернул ровно reader_limit — упёрся в потолок

    async def fake_client(customer_id=None):
        return object()

    async def fake_read(*args, **kwargs):
        return [
            SearchTermRow(
                f"term{i}", "Camp", "AdGroup", "kw", "BROAD", Metrics(10, 1, 1_000_000, 0, 0)
            )
            for i in range(n)
        ]

    async def fake_account_today(client, customer_id, *, label="acct_tz"):
        return date(2020, 6, 15)

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", fake_read)
    monkeypatch.setattr(rtz, "account_today", fake_account_today)

    with _read_allowed():
        env = asyncio.run(
            tr.get_search_terms(account=DRAFT_ACCOUNT_ID, period_days=7, reader_limit=n, limit=50)
        )
    assert env["error"] is None
    assert env["reader_capped"] is True and env["reader_limit"] == n


# ── list_accounts: перечисление дочерних MCC через обёртку (П15) ─────────────────────
def test_list_accounts_enumerates_children_offline(monkeypatch):
    """П15 «видит все дочерние MCC»: happy-path САМОЙ MCP-обёртки. Перечисление доказано на ads-слое
    (test_mcc_discovery), но конверт `list_accounts` (manager-замок + child_account_dict + пагинация)
    офлайн не прогонялся — форма ответа могла сломаться незамеченной. Замок ОБХОДА (не чтения) ⇒
    менеджерский id должен быть настроен."""

    async def fake_client(customer_id=None):
        return object()

    async def fake_read(*args, **kwargs):
        return [
            SimpleNamespace(
                id="111", name="Client A", currency="USD", manager=False, level=1, status="ENABLED"
            ),
            SimpleNamespace(
                id="222", name="Client B", currency="EUR", manager=False, level=1, status="ENABLED"
            ),
        ]

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", fake_read)

    with _manager_allowed(DRAFT_ACCOUNT_ID):
        env = asyncio.run(tr.list_accounts(manager_id=DRAFT_ACCOUNT_ID, limit=50))
    assert env["error"] is None and env["error_code"] is None  # замок обхода пройден
    assert env["total_rows"] == 2 and env["returned"] == 2 and env["truncated"] is False
    assert env["reader_capped"] is False  # у list_accounts нет GAQL-кэпа → сигнала нет
    assert [r["id"] for r in env["rows"]] == ["111", "222"]  # ОБА дочерних, ничего не потеряно
    assert env["rows"][0]["currency"] == "USD" and env["rows"][1]["name"] == "Client B"
    assert env["rows"][0]["manager"] is False and env["rows"][0]["level"] == 1


def test_list_accounts_denies_foreign_manager_offline(monkeypatch):
    """Обратный контроль к предыдущему: чужой (ненастроенный) менеджер ⇒ отказ замка ОБХОДА, а не
    перечисление. Без него happy-path остался бы зелёным при полностью снятом manager-замке."""

    async def fake_read(*args, **kwargs):  # не должен вызваться — замок раньше
        raise AssertionError("ридер не должен исполниться при отказе manager-замка")

    monkeypatch.setattr(tr, "run_ads_read_call", fake_read)

    with _manager_allowed("628-373-8601"):  # настроен ДРУГОЙ MCC
        env = asyncio.run(tr.list_accounts(manager_id="999-999-9999", limit=50))
    assert env["rows"] == [] and env["error_code"] == "forbidden_account"


# ── get_change_history: обёртка над НАШЕЙ БД (не SDK) ────────────────────────────────
def test_get_change_history_envelope_over_db_offline(monkeypatch):
    """Единственная READ-обёртка, не ходящая в Google Ads: `get_change_history` зовёт нашу БД
    (`list_recent_applied_by_customer`) и сериализует `recent_action_dict`. Проверяем, что замок
    ЧТЕНИЯ на границе (`_guarded`, И6 — у БД-ридера своего нет) пройден, аккаунт/operation/limit
    проброшены в ридер, а `decided_at` ушёл ISO-строкой в конверт."""
    captured: dict = {}

    async def fake_history(customer_id, *, operation=None, limit=20):
        captured.update(customer_id=customer_id, operation=operation, limit=limit)
        return [
            SimpleNamespace(
                confirmation_id="c1",
                operation="update_budget",
                params={"amount": 500},
                summary="поднять дневной бюджет",
                decided_at=datetime(2020, 6, 15, 12, 0),
            )
        ]

    monkeypatch.setattr(tr, "list_recent_applied_by_customer", fake_history)

    with _read_allowed():
        env = asyncio.run(
            tr.get_change_history(account=DRAFT_ACCOUNT_ID, operation="update_budget")
        )
    # замок границы пропустил ридер БЕЗ собственного замка (И6) на разрешённый аккаунт
    assert captured == {"customer_id": DRAFT_ACCOUNT_ID, "operation": "update_budget", "limit": 20}
    assert env["error"] is None and env["error_code"] is None
    assert env["total_rows"] == 1 and env["reader_capped"] is False  # 1 < reader_limit(20)
    assert env["rows"][0]["confirmation_id"] == "c1"
    assert env["rows"][0]["decided_at"] == "2020-06-15T12:00:00"  # datetime → ISO КОДОМ


def test_get_change_history_denies_foreign_account_offline(monkeypatch):
    """Обратный контроль: у БД-ридера замка нет — единственная защита — граница `_guarded`. Чужой
    аккаунт ⇒ forbidden_account ДО обращения к БД (иначе читались бы чужие применённые операции)."""

    async def fake_history(*args, **kwargs):  # не должен вызваться
        raise AssertionError("ридер БД не должен исполниться при отказе замка чтения")

    monkeypatch.setattr(tr, "list_recent_applied_by_customer", fake_history)

    with _read_allowed():  # разрешён DRAFT, спрашиваем ДРУГОЙ
        env = asyncio.run(tr.get_change_history(account="628-373-8601"))
    assert env["rows"] == [] and env["error_code"] == "forbidden_account"
