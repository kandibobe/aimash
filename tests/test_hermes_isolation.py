"""Гарды изоляции разрешений от памяти/скилов/external-контента (пивот Hermes, §4 — И1…И8).

Инкремент «MCP READ» несёт ТОЛЬКО read-релевантное зерно инвариантов; полные И1–И8 + injection-корпус
— шаг ПЕРЕД WRITE (docs/HERMES_SPEC.md §4, дорожная карта шаги 2/15). Здесь живыми проверяются:

  • **И4 (зерно)** — construction-time assert в `mcp_server.server`: READ-инструменты физически не
    пересекаются с мутационными (`agent.tools.schemas.MUTATION_TOOLS`). Импорт роняет процесс, если
    мутация просочилась в read-фазу. Тот же паттерн S4, что защищает `ANALYSIS_TOOLS`.
  • **read-lock на границе MCP** — READ-инструмент на аккаунте вне allow-list возвращает
    редактированный error-конверт (fail-closed, правило 5), НЕ сырое исключение и НЕ данные;
    на Draft (в потолке) замок ЧТЕНИЯ проходит.

Остальные И1–И3/И5–И8 — каркас со `skip("шаг перед WRITE")` с ДОСЛОВНОЙ формулировкой инварианта,
чтобы файл рос, а не переписывался, и следующий шаг наполнил их корпусом атак (инлайн, как
tests/test_dossier.py / test_export_formula_injection.py).

Стиль — как tests/test_safety_core.py: `sys.path.insert` + `# noqa: E402`, contextmanager-фикстуры
allow-list поверх `settings`, in-process, `asyncio.run` для async-инструментов.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID, ensure_read_allowed  # noqa: E402
from core.config import settings  # noqa: E402

# Аккаунт заведомо вне любого allow-list/потолка/грантов (не Draft, не MCC) — «чужой боевой id».
_FOREIGN_ID = "9999999999"


@contextmanager
def _allow_lists(*, mutate: str = "", read: str = ""):
    """Задать mutation- и read-allow-list поверх settings (перебивает значения из .env) — чтобы
    доступ был детерминирован независимо от локального окружения. Пустые оба ⇒ чтение запрещено
    даже Draft'у: замок ЧТЕНИЯ (ads.client.ensure_read_allowed) смотрит allowed∪read∪_READ_DISCOVERED,
    а НЕ мутационный ALLOWED_CEILING — Draft читается, лишь если он в одном из списков (в dev — есть)."""
    prev_mut = settings.google_ads_allowed_customer_ids
    prev_read = settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = mutate
    settings.google_ads_read_customer_ids = read
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev_mut
        settings.google_ads_read_customer_ids = prev_read


# ── И4 (зерно): READ-инструменты физически не пересекаются с мутационными ────────────
def test_i4_seed_read_tools_disjoint_from_mutations():
    # Импорт mcp_server.server несёт construction-time assert И4 — если он нарушен, импорт (а с ним
    # и весь MCP-процесс) падает здесь, а не «тихо открывает мутацию» в read-фазе.
    import mcp_server.server  # noqa: F401  — импорт ради живого assert И4

    from agent.tools.schemas import MUTATION_TOOLS
    from mcp_server.tools_read import READ_MCP_TOOLS

    assert READ_MCP_TOOLS, "реестр READ-инструментов пуст"
    assert READ_MCP_TOOLS.isdisjoint(MUTATION_TOOLS), (
        "И4: READ-инструменты MCP пересеклись с мутационными: "
        f"{sorted(READ_MCP_TOOLS & MUTATION_TOOLS)}"
    )
    assert len(READ_MCP_TOOLS) == 12, f"ожидалось 12 READ-инструментов, стало {len(READ_MCP_TOOLS)}"


def test_i4_seed_server_builds_and_registers_only_read():
    # Сервер строится и регистрирует РОВНО READ-набор — ни одного лишнего/мутационного имени.
    from mcp_server.server import build_server
    from mcp_server.tools_read import READ_MCP_TOOLS

    srv = build_server()
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == set(READ_MCP_TOOLS), f"реестр FastMCP разошёлся с READ_MCP_TOOLS: {names}"


# ── read-lock на границе MCP: отказ приходит редактированным конвертом, не данными ───
def test_read_tool_denies_foreign_account_via_error_envelope():
    # get_change_history зовёт ensure_read_allowed ПЕРВОЙ строкой (до SDK/БД) — поэтому тест офлайн
    # и детерминирован. Аккаунт вне allow-list ⇒ PermissionError внутри → _guarded → error-конверт.
    from mcp_server import tools_read as tr

    with _allow_lists(mutate="", read=""):  # оба пусты ⇒ любой аккаунт запрещён к чтению
        env = asyncio.run(tr.get_change_history(account=_FOREIGN_ID))

    assert env["rows"] == [], "fail-closed нарушен: отказ вернул данные"
    assert env["total_rows"] == 0
    assert env["error"], "ожидался редактированный текст отказа в error"
    # Правило 5: наружу только редактированный текст — не трасса и не сырой repr исключения.
    assert "Traceback" not in env["error"]


def test_read_lock_allows_draft_but_still_denies_foreign():
    # Обратная сторона fail-closed: с Draft в allow-list (как в dev .env) замок ЧТЕНИЯ его пропускает,
    # но чужой id при том же наборе всё равно отклонён — это замок, а не «всё открыто».
    with _allow_lists(mutate=DRAFT_ACCOUNT_ID, read=""):
        ensure_read_allowed(DRAFT_ACCOUNT_ID)  # не должно бросить
        try:
            ensure_read_allowed(_FOREIGN_ID)
            raise AssertionError("чужой аккаунт прошёл замок ЧТЕНИЯ — fail-open")
        except PermissionError:
            pass


# ── Каркас полных инвариантов И1–И8 (наполняется шагом ПЕРЕД WRITE) ──────────────────
# Формулировки — дословно из docs/HERMES_SPEC.md §4. Корпус атак (client_site_pages, скилы в
# shell/HTTP/файлы, страницы конкурентов) — инлайн, как в существующих injection-тестах.

_WRITE_STEP = "наполняется шагом ПЕРЕД WRITE (docs/HERMES_SPEC.md §4, дорожная карта шаги 2/15)"


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i1_external_source_cannot_change_proposal_customer_id():
    """И1: ни скил/память/факт/текст с сайта клиента не меняет customer_id в proposal; аккаунт берётся
    из proposal.customer_id и заново проходит ensure_allowed."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i2_injection_text_creates_no_proposal_and_no_user_initiated():
    """И2: внешний текст с инструкцией («игнорируй указания»/«подтверди операцию») не создаёт proposal
    и не выставляет user_initiated. Корпус атак, включая client_site_pages."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i3_user_initiated_only_from_human_whitelist_reply():
    """И3: user_initiated выставляется только по реплай-подтверждению живого человека из whitelist;
    ни скил, ни cron, ни self-improvement fork его не выставляют."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i5_self_written_skill_calls_only_registered_mcp_tools():
    """И5: самонаписанный скил не может вызвать ничего, кроме зарегистрированных MCP-инструментов.
    Корпус скилов, пытающихся выйти в shell/HTTP/файлы."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i6_memory_and_history_search_filtered_by_client_id():
    """И6: поиск по памяти и истории фильтруется по client_id текущего топика (изоляция клиента A от
    топика клиента B — session_search в Hermes ищет по всей БД)."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i7_external_content_taints_turn_and_disables_mutations():
    """И7: в ходе, где прочитан external-контент (страница/досье/CSV конкурентов), MCP-мутации
    физически недоступны; таинт по thread_id, снимается только сигналом нового человеческого сообщения."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i8_at_most_one_pending_proposal_per_turn():
    """И8: не более одного pending proposal на ассистентский ход. Энфорсмент — счётчик pending в нашем
    MCP-слое (на прогон/тред), не надежда на ограничения модели."""
