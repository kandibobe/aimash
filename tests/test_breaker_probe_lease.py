"""Гарды распределённого размыкателя (`core.breaker`, Волна 2).

Проверяются КЛАССЫ дефектов, а не строки:

1. **Аренда пробы не атомарна.** Если условия «окно остыло / аренда свободна» читать SELECT-ом, а
   писать отдельным UPDATE, то все, кто прочитал одновременно, решат, что пробник — они. Half-open
   выродится в тот же thundering herd, ради которого размыкатель и написан, — и выглядеть будет
   исправно (в одиночном тесте проба одна). Гард: N параллельных претендентов, ровно один пропущен.
2. **Инкремент через read-modify-write.** Два процесса, прочитав `failure_count=3`, оба запишут 4 —
   порог сдвинется вдвое, цепь не разомкнётся на объёме, ради которого она есть.
3. **Размыкание не по тем ошибкам.** Открыть цепь на `USER_PERMISSION_DENIED` = запереть исправный
   API из-за одного отключённого аккаунта; открыть на `DEADLINE_EXCEEDED`/`TimeoutError` = сделать
   вывод о Google из НАШЕГО дедлайна, при том что исход мутации неизвестен.
4. **Fail-closed при сбое стора.** Осознанное исключение из правила 10: недоступный стор обязан
   ПРОПУСКАТЬ вызовы. Размыкатель — про доступность; отказав закрыто, он сам станет аварией.
5. **Цепь застряла навсегда.** `opened_at IS NULL` при state='open' не должен запирать API.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core import breaker
from db.models import CircuitState
from db.session import Session, db_dt, init_db


@pytest.fixture(autouse=True)
async def _clean_circuits():
    """Цепи — общее состояние в БД; без сброса тесты видят чужие отказы (и наоборот).
    init_db() ДО reset(): reset — это DELETE FROM реальной таблицы (как в тестах квоты)."""
    await init_db()
    await breaker.reset()
    yield
    await breaker.reset()


async def _row(name: str) -> CircuitState | None:
    async with Session() as s:
        return (
            await s.execute(select(CircuitState).where(CircuitState.name == name))
        ).scalar_one_or_none()


async def _open_circuit(name: str, *, opened_ago_s: float) -> None:
    """Завести РАЗОМКНУТУЮ цепь с заданным возрастом размыкания (обход накопления отказов)."""
    now = datetime.now(timezone.utc)
    async with Session() as s:
        s.add(
            CircuitState(
                name=name,
                state="open",
                failure_count=breaker.FAILURE_THRESHOLD,
                opened_at=db_dt(now - timedelta(seconds=opened_ago_s)),
                updated_at=db_dt(now),
            )
        )
        await s.commit()


class _Transient(Exception):
    """Дакт-фейк GoogleAdsException: core.ads_errors читает failure.errors[].error_code.name."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.failure = type("F", (), {"errors": [_Err(code)]})()


class _Err:
    def __init__(self, code: str) -> None:
        self.error_code = type("C", (), {"name": code})()


# ── 1. Аренда пробы: ровно один из N ─────────────────────────────────────────────


async def test_probe_lease_grants_exactly_one_of_many():
    """N одновременных претендентов на half-open → пропущен РОВНО ОДИН, остальные отсечены.

    Претенденты синхронизируются `gate`, и это не украшение: без него победитель успевает завершить
    пробу и ЗАМКНУТЬ цепь, после чего опоздавший видит уже `closed` и проходит законно — тест ловил
    бы порядок планировщика, а не эксклюзивность аренды (наблюдалось: 2 из 8). Каждый претендент
    отмечается сразу после своего `_check`, победитель держит аренду внутри `guard`, пока не
    отметятся все восемь."""
    name = "ads:probe-race"
    await _open_circuit(name, opened_ago_s=breaker.OPEN_COOLDOWN_S + 5)
    checked = 0
    gate = asyncio.Event()

    def _mark() -> None:
        nonlocal checked
        checked += 1
        if checked == 8:
            gate.set()

    async def _try() -> bool:
        try:
            async with breaker.guard(name):
                _mark()
                await asyncio.wait_for(gate.wait(), 5.0)  # «пробный вызов» на время всей гонки
            return True
        except breaker.CircuitOpenError:
            _mark()
            return False

    results = await asyncio.gather(*[_try() for _ in range(8)])
    assert sum(results) == 1, (
        f"аренду пробы получили {sum(results)} из 8 — half-open выродился в thundering herd. "
        "Условия аренды обязаны проверяться и записываться ОДНИМ UPDATE (rowcount==1)."
    )


async def test_probe_success_closes_circuit_for_everyone():
    """Успешная проба замыкает цепь: следующие вызовы проходят без аренды."""
    name = "ads:probe-ok"
    await _open_circuit(name, opened_ago_s=breaker.OPEN_COOLDOWN_S + 5)

    async with breaker.guard(name):
        pass
    row = await _row(name)
    assert row is not None and row.state == "closed" and row.failure_count == 0
    assert row.probe_lease_until is None

    async with breaker.guard(name):  # уже никакой аренды не нужно
        pass


async def test_failed_probe_reopens_and_restarts_cooldown():
    """Упавшая проба размыкает цепь заново и сдвигает окно остывания (иначе herd на каждом тике)."""
    name = "ads:probe-fail"
    await _open_circuit(name, opened_ago_s=breaker.OPEN_COOLDOWN_S + 5)

    with pytest.raises(_Transient):
        async with breaker.guard(name):
            raise _Transient("RESOURCE_EXHAUSTED")

    row = await _row(name)
    assert row is not None and row.state == "open"
    assert row.probe_lease_until is None, (
        "аренда обязана освобождаться — иначе цепь встанет навсегда"
    )
    # Окно отсчитывается заново: следующий претендент отсекается.
    with pytest.raises(breaker.CircuitOpenError):
        async with breaker.guard(name):
            pass


async def test_open_circuit_refuses_before_cooldown():
    """Окно остывания не истекло — вызов отсечён БЕЗ похода в сеть (тело не исполняется)."""
    name = "ads:cooling"
    await _open_circuit(name, opened_ago_s=1.0)
    ran = False
    with pytest.raises(breaker.CircuitOpenError):
        async with breaker.guard(name):
            ran = True
    assert not ran, "тело вызова исполнилось на разомкнутой цепи — размыкатель не размыкает"


async def test_stuck_open_without_opened_at_still_probes():
    """state='open' с opened_at IS NULL не запирает API навсегда (NULL <= x даёт NULL, не TRUE)."""
    name = "ads:stuck"
    now = datetime.now(timezone.utc)
    async with Session() as s:
        s.add(CircuitState(name=name, state="open", failure_count=99, updated_at=db_dt(now)))
        await s.commit()
    async with breaker.guard(name):  # проба выдана, цепь восстановима
        pass
    row = await _row(name)
    assert row is not None and row.state == "closed"


# ── 2. Порог: инкремент атомарный, размыкание на пороге ───────────────────────────


async def test_threshold_opens_circuit_and_counter_is_atomic():
    """FAILURE_THRESHOLD отказов подряд размыкают цепь; счётчик считается в SQL, не в питоне."""
    name = "ads:threshold"
    for i in range(breaker.FAILURE_THRESHOLD):
        with pytest.raises(_Transient):
            async with breaker.guard(name):
                raise _Transient("RATE_EXCEEDED")
        row = await _row(name)
        assert row is not None and row.failure_count == i + 1

    row = await _row(name)
    assert row is not None and row.state == "open" and row.opened_at is not None
    with pytest.raises(breaker.CircuitOpenError):
        async with breaker.guard(name):
            pass


async def test_concurrent_failures_are_not_lost():
    """Параллельные отказы не затирают друг друга (read-modify-write потерял бы часть)."""
    name = "ads:concurrent-fail"
    # Порог поднимаем на время теста, иначе цепь разомкнётся на середине и остальные отказы
    # приедут уже как CircuitOpenError (и учитываться не должны — это не новое свидетельство).
    original = breaker.FAILURE_THRESHOLD
    breaker.FAILURE_THRESHOLD = 100
    try:

        async def _fail() -> None:
            with pytest.raises(_Transient):
                async with breaker.guard(name):
                    raise _Transient("TRANSIENT_ERROR")

        await asyncio.gather(*[_fail() for _ in range(6)])
    finally:
        breaker.FAILURE_THRESHOLD = original
    row = await _row(name)
    assert row is not None and row.failure_count == 6, (
        f"учтено {row.failure_count if row else 0} из 6 отказов — инкремент теряется в гонке; "
        "он обязан считаться одним UPDATE (failure_count = failure_count + 1)"
    )


async def test_success_resets_counter():
    """Успех обнуляет накопленные отказы — порог именно «подряд», а не «всего за всё время»."""
    name = "ads:reset-on-ok"
    with pytest.raises(_Transient):
        async with breaker.guard(name):
            raise _Transient("RESOURCE_EXHAUSTED")
    async with breaker.guard(name):
        pass
    row = await _row(name)
    assert row is not None and row.failure_count == 0 and row.state == "closed"


# ── 3. Классификация: что НЕ размыкает цепь ──────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        _Transient("USER_PERMISSION_DENIED"),  # аккаунт отключён — состояние аккаунта, не сервиса
        _Transient("CUSTOMER_NOT_ENABLED"),
        _Transient("INTERNAL_ERROR"),  # исход неизвестен: мутация могла примениться
        _Transient("DEADLINE_EXCEEDED"),
        _Transient("FIELD_NOT_FOUND"),  # наш дефект — размыкатель его замаскировал бы
        TimeoutError(),  # НАШ asyncio.timeout, не свидетельство о Google
        ValueError("кривой аргумент"),
    ],
    ids=[
        "permission_denied",
        "customer_not_enabled",
        "internal_error",
        "deadline_exceeded",
        "field_not_found",
        "our_timeout",
        "value_error",
    ],
)
async def test_non_outage_errors_never_open_circuit(exc):
    assert breaker.counts_as_failure(exc) is False
    name = "ads:non-outage"
    for _ in range(breaker.FAILURE_THRESHOLD + 2):
        with pytest.raises(type(exc)):
            async with breaker.guard(name):
                raise exc
    row = await _row(name)
    assert row is None or (row.state == "closed" and row.failure_count == 0), (
        "цепь учла отказ, который не говорит о недоступности сервиса"
    )


@pytest.mark.parametrize(
    "code",
    ["RESOURCE_EXHAUSTED", "RATE_EXCEEDED", "RESOURCE_TEMPORARILY_EXHAUSTED", "TRANSIENT_ERROR"],
)
def test_outage_codes_count(code):
    assert breaker.counts_as_failure(_Transient(code)) is True


def test_transport_503_and_429_count():
    from google.api_core import exceptions as gapi

    assert breaker.counts_as_failure(gapi.ServiceUnavailable("503")) is True
    assert breaker.counts_as_failure(gapi.TooManyRequests("429")) is True
    # 500/deadline — «исход неизвестен», не «сервис лежит»
    assert breaker.counts_as_failure(gapi.InternalServerError("500")) is False
    assert breaker.counts_as_failure(gapi.DeadlineExceeded("deadline")) is False


def test_llm_provider_errors_count():
    import httpx
    from openai import APIConnectionError, APITimeoutError

    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    assert breaker.counts_as_failure(APIConnectionError(request=req)) is True
    assert breaker.counts_as_failure(APITimeoutError(request=req)) is True


# ── 4. Сбой стора → fail-OPEN (исключение из правила 10) ─────────────────────────


async def test_store_failure_lets_call_through(monkeypatch):
    """Недоступный стор НЕ блокирует вызовы: осознанное исключение из правила 10 (доступность).

    Здесь fail-open — не послабление безопасности: замок аккаунта, confirm-гейт, провенанс и квота
    стоят отдельно и fail-closed каждый. Отказ закрыто означал бы «не применили миграцию 0033 ⇒
    встал весь Google Ads»."""

    def _boom(*a, **kw):
        raise RuntimeError("стор недоступен")

    monkeypatch.setattr(breaker, "Session", _boom)
    ran = False
    async with breaker.guard("ads:no-store"):
        ran = True
    assert ran


# ── 5. Интеграция: инструмент MCP отдаёт честный код ─────────────────────────────


def test_circuit_open_classified_as_upstream_error():
    """Отсечённый вызов — отказ ДАЛЬНЕЙ стороны, а не наш `internal` (набор ERROR_CODES заморожен)."""
    from mcp_server.envelope import ERROR_CODES, classify_error

    code = classify_error(breaker.CircuitOpenError("канал ads:1 временно отключён"))
    assert code == "upstream_error"
    assert code in ERROR_CODES


async def test_resilience_read_wrapper_is_guarded():
    """Обёртка чтения реально ходит через размыкатель (иначе интеграции нет — только модуль)."""
    from core import resilience

    name = breaker.circuit_name("ads", "7753643025")
    await _open_circuit(name, opened_ago_s=1.0)
    called = False

    def _reader():
        nonlocal called
        called = True
        return []

    with pytest.raises(breaker.CircuitOpenError):
        await resilience.run_ads_read_call(_reader, account="7753643025")
    assert not called, "SDK-вызов ушёл в сеть на разомкнутой цепи"


async def test_circuit_is_per_account():
    """Цепь пер-аккаунт: разомкнутая на одном не запирает остальные 16."""
    from core import resilience

    await _open_circuit(breaker.circuit_name("ads", "111"), opened_ago_s=1.0)
    assert await resilience.run_ads_read_call(lambda: "ok", account="222") == "ok"


async def test_open_llm_circuit_degrades_to_fallback_model(monkeypatch):
    """Разомкнутая цепь ОСНОВНОЙ модели уводит вызов на резервную, а не роняет `chat()`.

    Это не косметика: без разбора `CircuitOpenError` в `agent/router.py` размыкатель на модели A
    ронял бы парсинг целиком — при живой модели B. Добавить цепь в `_is_retryable_llm` нельзя (тот же
    предикат — условие ретрая tenacity), поэтому решение принимается в ветке деградации, и гард
    стоит здесь."""
    from agent import router as R

    used: list[str] = []

    def _resp():  # минимальная форма ответа SDK: chat() читает .choices[0].message/.usage
        msg = type("Msg", (), {"content": "ok", "tool_calls": None})()
        choice = type("Ch", (), {"message": msg, "finish_reason": "stop"})()
        return type("R", (), {"choices": [choice], "usage": None})()

    async def _fake_call_llm(factory, label=None, circuit=None):
        used.append(circuit or "")
        async with breaker.guard(breaker.circuit_name("llm", circuit)):
            return _resp()

    await _open_circuit(breaker.circuit_name("llm", "primary/model"), opened_ago_s=1.0)
    monkeypatch.setattr(R, "call_llm", _fake_call_llm)
    monkeypatch.setattr(R, "_client", lambda: None)
    monkeypatch.setattr(R, "_active_model", None)
    monkeypatch.setattr(
        R, "ROLE_MODELS", {**R.ROLE_MODELS, "parsing": "primary/model", "fallback": "spare/model"}
    )

    msg = await R.chat([{"role": "user", "content": "x"}], role="parsing")
    assert msg.content == "ok"
    assert used == ["primary/model", "spare/model"], (
        f"вместо деградации на резервную модель получили {used} — разомкнутая цепь основной "
        "модели не должна ронять chat() при живой резервной"
    )


async def test_reset_is_sqlite_only(monkeypatch):
    """Предохранитель: reset() — это DELETE; вне SQLite он стёр бы живое состояние размыкателя."""

    class _PG:
        name = "postgresql"

    monkeypatch.setattr(breaker.engine, "dialect", _PG())
    with pytest.raises(RuntimeError):
        await breaker.reset()
