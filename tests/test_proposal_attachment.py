"""Волна 1b: обещанное .xlsx-вложение черновика обязано БЫТЬ.

Живой дефект, который здесь заколачивается. `confirm/render.py` печатал человеку «…ещё N — полный
список во вложении .xlsx» для ШЕСТИ операций, а отправлял файл `bot/main.py::_KEYWORD_OPS` для
ЧЕТЫРЁХ: `add_negatives_to_shared_set` получал обещание и не получал файла. Текст обещания при этом
уезжал в `summary` черновика, то есть в audit-row, из которого правило 15 репортит «выполнено» —
человек жал ✅ на объёме, которого не видел, а журнал подтверждал, что всё показано.

Ключевая инвариантa всего файла: **обещание печатается тогда и только тогда, когда план вложения
собран**. Проверяется не «где-то есть set с шестью именами», а именно эквивалентность двух решений
на всех операциях сразу — потому что дефект был ровно в том, что решения было ДВА.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from confirm import render  # noqa: E402
from confirm.attachment import (  # noqa: E402
    KEYWORD_XLSX_OPS,
    MAX_ATTACHMENT_ROWS,
    plan_attachment,
    safe_filename,
)
from confirm.store import ConfirmStore  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DRAFT = "7753643025"
CID = "abc123def456"


def _kws(n: int) -> list[str]:
    return [f"ключ {i}" for i in range(n)]


def _big(op: str = "add_keywords", **extra) -> dict:
    return {"keywords": _kws(render.KW_INLINE_MAX + 5), "match_type": "EXACT", **extra}


# ─────────────────────────── обещание ⟺ план ───────────────────────────


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("op", sorted(KEYWORD_XLSX_OPS))
def test_promise_iff_plan(op: str, lang: str):
    """Для каждой операции набора: текст обещает вложение ровно тогда, когда план не None.

    Это и есть тот самый инвариант — не «список правильный», а «двух решений нет». Если кто-то
    добавит операцию в KEYWORD_XLSX_OPS, не научив рендер её печатать (или наоборот), тест краснеет.
    """
    params = _big(op)
    spec = plan_attachment(op, params, cid=CID, lang=lang)
    assert spec is not None, f"{op}: план вложения не собрался на большом списке"
    text = render.fmt_mutation_summary(op, params, lang, attachment=True)
    if not text:
        # У операции своя богатая карточка (`core.texts.fmt_search_proposal_summary`, кнопочный
        # визард): рендер честно отдаёт "", текст собирает вызывающий. Файл при этом уходит —
        # «вложение без обещания» не ложь и не предмет этого файла, лгало обратное.
        pytest.skip(f"{op}: рендер отдаёт свою богатую карточку — обещание печатает вызывающий")
    marker = ".xlsx" if lang == "en" else ".xlsx"
    assert marker in text, f"{op}/{lang}: план есть, обещания в тексте нет: {text!r}"


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("op", sorted(KEYWORD_XLSX_OPS))
def test_no_promise_without_attachment_flag(op: str, lang: str):
    """attachment=False ⇒ ни слова про вложение, но усечение всё равно видно («…ещё N»).

    Обратная половина: без неё «печатать обещание всегда» удовлетворило бы тест выше. И отдельно —
    что усечение остаётся ЗАМЕТНЫМ: молча показать 20 из 25 хуже, чем не приложить файл."""
    text = render.fmt_mutation_summary(op, _big(op), lang, attachment=False)
    if not text:
        pytest.skip(f"{op}: своя карточка")
    assert ".xlsx" not in text, f"{op}/{lang}: обещание без плана: {text!r}"
    assert ("ещё 5" in text) or ("5 more" in text), f"{op}/{lang}: усечение не показано: {text!r}"


def test_shared_set_op_covered_everywhere():
    """`add_negatives_to_shared_set` — та самая операция, из-за которой всё это написано.

    Проверяется в трёх местах сразу: набор, план (со `scope` из shared_set — у неё нет кампании) и
    подпись действия. Именной тест, а не строчка в параметризации: регрессия сюда уже приезжала."""
    assert "add_negatives_to_shared_set" in KEYWORD_XLSX_OPS
    spec = plan_attachment(
        "add_negatives_to_shared_set",
        _big("add_negatives_to_shared_set", shared_set="Общие минуса"),
        cid=CID,
        lang="ru",
    )
    assert spec is not None
    assert spec.scope == "Общие минуса", "лист не отвечает, куда лягут минус-слова"
    assert spec.action and spec.action != "add_negatives_to_shared_set", "нет человеческой подписи"


def test_single_threshold_source():
    """Порог усечения у рендера и у плана — ОДИН объект, не две одинаковые константы.

    Двигаем `render.KW_INLINE_MAX` и требуем, чтобы план поехал вместе с ним. Разъехавшиеся пороги
    дают либо обещание «ещё 3» без файла, либо файл без обещания."""
    lo = 3
    orig = render.KW_INLINE_MAX
    try:
        render.KW_INLINE_MAX = lo
        assert plan_attachment("add_keywords", {"keywords": _kws(lo)}, cid=CID, lang="ru") is None
        assert (
            plan_attachment("add_keywords", {"keywords": _kws(lo + 1)}, cid=CID, lang="ru")
            is not None
        )
    finally:
        render.KW_INLINE_MAX = orig


@pytest.mark.parametrize(
    "op,params",
    [
        ("update_budget", {"keywords": _kws(50)}),  # операция вне набора
        ("add_keywords", {"keywords": _kws(3)}),  # список влезает в карточку
        ("add_keywords", {"keywords": "не список"}),  # мусорный тип от модели
        ("add_keywords", {}),  # ключей нет вовсе
        ("add_keywords", None),  # params не dict
    ],
)
def test_plan_returns_none(op: str, params):
    """Отказ собирать план — молчаливый None, не исключение: политику зовут на КАЖДОМ черновике,
    и падение здесь уронило бы показ карточки операции, которой вложение вообще не нужно."""
    assert plan_attachment(op, params, cid=CID, lang="ru") is None


# ─────────────────────────── граница слоёв ───────────────────────────


def test_policy_module_is_pure():
    """`confirm/attachment.py` не тянет openpyxl / aiogram / ФС — ни на уровне модуля, ни лениво.

    Разбором AST, а не по `sys.modules`: ленивый `import openpyxl` внутри функции зонд по модулям не
    увидит в принципе. Причина запрета конкретная — политику импортирует MCP-процесс, который
    стартует по `docker exec` и уже упирался в `mcp_discovery_timeout` на холодном старте."""
    tree = ast.parse((ROOT / "confirm" / "attachment.py").read_text(encoding="utf-8"))
    banned = ("openpyxl", "aiogram", "keywords.export", "os.path", "tempfile", "bot")
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
    assert not bad, f"политика вложения тянет запрещённое: {bad}"


def test_import_policy_does_not_load_openpyxl():
    """Тот же запрет, но проверенный ИСПОЛНЕНИЕМ: чистый процесс импортирует политику и openpyxl в
    `sys.modules` не появляется. AST ловит текст, этот — транзитивную тягу через зависимости."""
    code = (
        "import sys; import confirm.attachment;"
        "assert 'openpyxl' not in sys.modules, 'openpyxl подтянулся транзитивно';"
        "assert 'aiogram' not in sys.modules, 'aiogram подтянулся транзитивно'"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


def test_mcp_envelope_carries_no_bytes():
    """Тул-слой не отдаёт файл в MCP-конверте. base64 — это ВЕСЬ файл через окно модели (+33% и
    платная запись кэша 1.25×), поэтому решение «вложение доставляет отдельный процесс» защищено
    тестом, а не комментарием."""
    src = (ROOT / "mcp_server" / "propose.py").read_text(encoding="utf-8")
    for bad in ("b64encode", "base64", "BufferedInputFile", "openpyxl"):
        assert bad not in src, f"mcp_server/propose.py тащит байты вложения: {bad}"


# ─────────────────────────── сборка файла ───────────────────────────


def test_workbook_rows_capped_and_truncation_visible():
    """Потолок строк соблюдён, а усечение НЕ молчит: и в листе пометка, и в спеке полное `total`.

    Потолок здесь не про красоту: Telegram отбивает документ >50 МБ, и вставленный из CSV список на
    200k минус-слов давал бы обещание без файла — ровно тот дефект, который волна чинит."""
    from keywords.export import build_keyword_list_workbook

    n = render.KW_INLINE_MAX + 5
    wb = build_keyword_list_workbook(_kws(n), "EXACT", "Добавить", max_rows=5)
    ws = wb.active
    texts = [str(c.value or "") for row in ws.iter_rows() for c in row]
    assert any("усечён" in t for t in texts), "усечение молчит"
    assert sum(1 for t in texts if t.startswith("ключ ")) == 5, "потолок строк не соблюдён"
    spec = plan_attachment("add_keywords", {"keywords": _kws(n)}, cid=CID, lang="ru")
    assert spec is not None and spec.total == n, "спека потеряла полное число ключей"


def test_workbook_cells_are_redacted():
    """Ячейки проходят `redact_text` (правило 5): .xlsx уезжает в Telegram НАВСЕГДА и отзыву не
    подлежит. `safe_row` рядом занимается формула-инъекцией — другой класс, не замена."""
    from keywords.export import build_keyword_list_workbook

    # OAuth refresh-token формы `1//…` — паттерн core.logging (не настоящий токен, синтетика).
    secret = "1//0gTESTVALUEabcdefghijklmnopqrstuvwxyz0123456789"
    wb = build_keyword_list_workbook([secret, "обычный ключ"], "EXACT", "Добавить")
    dump = "\n".join(str(c.value or "") for row in wb.active.iter_rows() for c in row)
    assert secret not in dump, "секрет уехал в .xlsx нередактированным"


def test_build_returns_workbook_not_path():
    """`build_keyword_list_workbook` отдаёт Workbook. Путь на ФС границу процессов не переживает:
    решение приложить принимает тул-слой в одном контейнере, собирает файл курьер — в другом."""
    from openpyxl.workbook import Workbook

    from keywords.export import build_keyword_list_workbook

    assert isinstance(build_keyword_list_workbook(["a"], "EXACT", "Добавить"), Workbook)


@pytest.mark.parametrize(
    "raw,expect_not",
    [("../../etc/passwd", "/"), ("..\\win", "\\"), ("add_keywords", " ")],
)
def test_safe_filename_has_no_separators(raw: str, expect_not: str):
    out = safe_filename(raw)
    assert expect_not not in out and out, f"{raw!r} → {out!r}"


def test_filename_unique_per_proposal():
    """Два черновика одной операции дают РАЗНЫЕ имена файлов: в чате они иначе неразличимы, а
    менеджер решает по тому, который открыл."""
    a = plan_attachment("add_keywords", _big(), cid="aaaaaaaaaaaa", lang="ru")
    b = plan_attachment("add_keywords", _big(), cid="bbbbbbbbbbbb", lang="ru")
    assert a and b and a.filename != b.filename


def test_max_rows_default_is_the_policy_constant():
    """Потолок берётся из политики, а не из своей константы в экспортёре."""
    import inspect

    from keywords.export import build_keyword_list_workbook

    sig = inspect.signature(build_keyword_list_workbook)
    assert sig.parameters["max_rows"].default == MAX_ATTACHMENT_ROWS


# ─────────────────────────── персистентность и одноразовость ───────────────────────────


@pytest.mark.asyncio
async def test_promise_and_obligation_written_in_one_insert():
    """Обещание (в summary) и обязательство (attachment_state) рождаются ОДНОЙ вставкой.

    Смысл не в «поле сохранилось», а в том, что расхождение невозможно: нет второго места и нет
    второго момента, где одно уже записано, а другое ещё нет (процесс мог там и умереть)."""
    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    params = _big()
    spec = plan_attachment("add_keywords", params, cid=cid, lang="ru")
    summary = render.fmt_mutation_summary("add_keywords", params, "ru", attachment=spec is not None)
    await store.save_proposal(
        confirmation_id=cid,
        operation="add_keywords",
        customer_id=DRAFT,
        params=params,
        summary=summary,
        chat_id=1,
        user_initiated=True,
        attachment_state="pending" if spec else None,
    )
    async with Session() as s:
        row = (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()
    assert (".xlsx" in row.summary) == (row.attachment_state == "pending"), (
        "обещание и обязательство разъехались в одной строке"
    )


@pytest.mark.asyncio
async def test_claim_attachment_is_single_shot():
    """CAS-клейм вложения одноразов: второй вызов False. Два планировщика (или один после рестарта)
    не пришлют файл дважды — одноразовость свойство ХРАНИЛИЩА, а не аккуратности курьера."""
    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="add_keywords",
        customer_id=DRAFT,
        params=_big(),
        summary="s",
        chat_id=1,
        user_initiated=True,
        attachment_state="pending",
    )
    assert await store.claim_attachment(cid) is True
    assert await store.claim_attachment(cid) is False
    await store.finish_attachment(cid, "sent")
    assert await store.claim_attachment(cid) is False, "'sent' снова застолбили"


@pytest.mark.asyncio
async def test_pending_list_ignores_rows_without_promise():
    """В очередь курьера попадают ТОЛЬКО строки с обещанием. Иначе он слал бы файлы к черновикам,
    в тексте которых про вложение не было ни слова."""
    await init_db()
    store = ConfirmStore()
    quiet, loud = uuid.uuid4().hex, uuid.uuid4().hex
    for cid, state in ((quiet, None), (loud, "pending")):
        await store.save_proposal(
            confirmation_id=cid,
            operation="add_keywords",
            customer_id=DRAFT,
            params=_big(),
            summary="s",
            chat_id=1,
            user_initiated=True,
            attachment_state=state,
        )
    cids = {p.confirmation_id for p in await store.list_pending_attachments(limit=100)}
    assert loud in cids and quiet not in cids


# ─────────────────────────── курьер ───────────────────────────


def test_courier_builds_file_off_event_loop():
    """Сборка .xlsx уходит в `asyncio.to_thread`. openpyxl CPU-bound, и список на тысячи ключей,
    собранный в корутине, держал бы event loop планировщика вместе со ВСЕМИ его джобами.

    Разбором AST: проверяем, что вызов сборщика стоит именно под `to_thread`, а не просто что где-то
    в файле встречается это имя."""
    tree = ast.parse((ROOT / "scheduler" / "jobs.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "deliver_proposal_attachments"
    )
    threaded = [
        c
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "to_thread"
        and any(isinstance(a, ast.Name) and "attachment" in a.id for a in c.args)
    ]
    assert threaded, "сборка .xlsx осталась в event loop планировщика"


def test_courier_never_sends_raw_exception_text():
    """Курьер логирует ТИП исключения, а не `str(e)` (правило 5: исключение google-ads/OpenAI может
    нести токен). Проверяем текстом — форматов у log.warning немного, а класс дорогой."""
    src = (ROOT / "scheduler" / "jobs.py").read_text(encoding="utf-8")
    fn_src = src[src.index("async def deliver_proposal_attachments") :]
    fn_src = fn_src[: fn_src.index("\nasync def cleanup_stale_proposals")]
    assert "type(e).__name__" in fn_src, "курьер не логирует тип исключения"
    assert ", e)" not in fn_src.replace("type(e)", ""), "сырое исключение уходит в лог"


def test_transport_does_not_own_file_lifecycle():
    """`send_bot_file` не удаляет файл: владелец — тот, кто его создал. Иначе при исключении на
    отправке владелец не знает, остался файл на диске или нет."""
    src = (ROOT / "scheduler" / "transport.py").read_text(encoding="utf-8")
    fn = src[src.index("async def send_bot_file") :]
    fn = fn[: fn.index("\nasync def send_bot_document")]
    assert "os.remove" not in fn and "tempfile" not in fn


@pytest.mark.asyncio
async def test_courier_removes_tempfile_when_send_fails(monkeypatch, tmp_path):
    """Отправка упала — временный файл всё равно удалён, строка помечена 'failed'.

    Без этого недоступный чат оставлял бы .xlsx на диске контейнера навсегда: файлов много, диск
    один, а заметить утечку некому."""
    import scheduler.jobs as J

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="add_keywords",
        customer_id=DRAFT,
        params=_big(),
        summary="s",
        chat_id=777,
        user_initiated=True,
        attachment_state="pending",
    )
    seen: list[str] = []

    def _fake_build(spec, lang="ru"):  # lang — Волна 5: подписи графика L3 выбираются при сборке
        p = tmp_path / f"{spec.filename}"
        p.write_bytes(b"x")
        seen.append(str(p))
        return str(p)

    async def _boom(bot, chat_id, *, path, filename):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(J, "_build_attachment_file", _fake_build)
    monkeypatch.setattr("scheduler.transport.send_bot_file", _boom)
    sent = await J.deliver_proposal_attachments(object(), limit=10)
    assert sent == 0
    assert seen and not Path(seen[0]).exists(), "временный файл остался после сбоя отправки"
    async with Session() as s:
        row = (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()
    assert row.attachment_state == "failed"


@pytest.mark.asyncio
async def test_courier_delivers_and_marks_sent(monkeypatch, tmp_path):
    """Счастливый путь целиком: строка выбрана, файл собран из ПЕРСИСТЕНТНЫХ params, отправлен под
    именем из спеки, помечен 'sent' и в очередь больше не попадает."""
    import scheduler.jobs as J

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="add_negatives_to_shared_set",
        customer_id=DRAFT,
        params=_big("add_negatives_to_shared_set", shared_set="Общие минуса"),
        summary="s",
        chat_id=777,
        user_initiated=True,
        attachment_state="pending",
    )
    calls: list[tuple] = []

    async def _ok(bot, chat_id, *, path, filename):
        assert Path(path).exists(), "курьер отдал транспорту несуществующий путь"
        calls.append((chat_id, filename))

    monkeypatch.setattr("scheduler.transport.send_bot_file", _ok)
    sent = await J.deliver_proposal_attachments(object(), limit=10)
    assert sent == 1 and calls and calls[0][0] == 777
    assert calls[0][1].endswith(".xlsx") and "add_negatives" in calls[0][1]
    async with Session() as s:
        row = (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()
    assert row.attachment_state == "sent"
    assert not await store.list_pending_attachments(limit=100) or cid not in {
        p.confirmation_id for p in await store.list_pending_attachments(limit=100)
    }
