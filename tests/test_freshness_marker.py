"""Волна 1.1, шаг 3: аттестация свежести на КАЖДОМ черновике и её несъедобность для повтора.

Маркер `_freshness` — единственное, что отличает «прочитали и сверили» от «не читали вовсе».
Поэтому он обязан быть:
  · ВСЕГДА (в т.ч. когда снимок не удался) — иначе «нет маркера» станет обычным состоянием и
    fail-closed-гейт придётся ослабить, чтобы бот вообще работал;
  · СВОЙ у каждого хода — шаблон/повтор/клон, унёсшие чужой маркер, дают гейту ложную аттестацию:
    черновик выглядит прочитанным, хотя аккаунт с тех пор никто не читал.

Тесты денежного пути (гейты A и B, потребители маркера) — tests/test_freshness_registry.py и
tests/test_write_layer.py; здесь проверяется только ПРОСТАВЛЕНИЕ и СНЯТИЕ.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.service as svc  # noqa: E402
import bot.main as bm  # noqa: E402
from ads.freshness import ATTESTATION_KEYS, strip_attestation  # noqa: E402
from db.history import _clean  # noqa: E402


# ── attach_freshness: маркер всегда, снимок — только при успешном чтении ─────────────────
def test_ok_snapshot_gives_marker_and_before():
    snap = svc.Snapshot(svc.SnapshotKind.OK, data={"kind": "budget", "before_micros": [40_000_000]})
    out = svc.attach_freshness({"campaign": "X"}, snap)
    assert out["_freshness"] == {"kind": "ok", "reason": ""}
    assert out["_before"] == {"kind": "budget", "before_micros": [40_000_000]}
    assert out["campaign"] == "X"  # прикладные ключи не тронуты


def test_unreadable_snapshot_still_gives_marker_but_no_before():
    """Чтение сорвалось — карточка всё равно показывается (сегодняшнее поведение), но черновик
    несёт ЧЕСТНОЕ 'не прочитали'. Отсутствие ключа `_before` само по себе неотличимо от NO_DIFF."""
    out = svc.attach_freshness(
        {"campaign": "X"}, svc.Snapshot(svc.SnapshotKind.UNREADABLE, reason="TimeoutError")
    )
    assert out["_freshness"] == {"kind": "unreadable", "reason": "TimeoutError"}
    assert "_before" not in out


def test_no_diff_snapshot_is_marked_distinctly_from_unreadable():
    """Ровно та развилка, ради которой заведён Snapshot: 'сверять нечего' ≠ 'сверка не состоялась'."""
    out = svc.attach_freshness(
        {"name": "новая кампания"}, svc.Snapshot(svc.SnapshotKind.NO_DIFF, reason="not_diffable")
    )
    assert out["_freshness"]["kind"] == "no_diff"
    assert out["_freshness"]["kind"] != "unreadable"
    assert "_before" not in out


def test_attach_does_not_mutate_caller_params():
    src = {"campaign": "X"}
    svc.attach_freshness(src, svc.Snapshot(svc.SnapshotKind.OK, data={"kind": "bid"}))
    assert src == {"campaign": "X"}  # вызывающий переиспользует свой dict в других ветках


def test_marker_never_carries_snapshot_values_of_other_accounts():
    """Маркер — это два коротких служебных поля, а не свалка: причина и класс. Значения из аккаунта
    живут в `_before` и уезжают только при OK (правило 5: наружу — ничего лишнего)."""
    out = svc.attach_freshness(
        {}, svc.Snapshot(svc.SnapshotKind.UNREADABLE, reason="PermissionError")
    )
    assert set(out["_freshness"]) == {"kind", "reason"}


# ── strip_attestation: снятие для ЛЮБОГО переиспользования params ───────────────────────
def test_strip_removes_both_attestation_keys_and_keeps_the_rest():
    params = {"campaign": "X", "value": 48, "_before": {"kind": "budget"}, "_freshness": {"k": 1}}
    assert strip_attestation(params) == {"campaign": "X", "value": 48}


def test_strip_does_not_mutate_input_and_tolerates_none():
    src = {"campaign": "X", "_freshness": {}}
    strip_attestation(src)
    assert "_freshness" in src
    assert strip_attestation(None) == {}


def test_recent_replay_strips_attestation():
    """/recent пере-собирает params применённой операции. Унеси он маркер — повтор проехал бы гейт
    на аттестации, выданной вчерашнему чтению."""
    cleaned = _clean({"campaign": "X", "_before": {"kind": "budget"}, "_freshness": {"kind": "ok"}})
    assert not (ATTESTATION_KEYS & set(cleaned))


# ── интеграция: карточка бота штампует маркер всегда ────────────────────────────────────
class _FakeMsg:
    def __init__(self, chat_id: int = 991001):
        self.answers: list = []
        self.chat = SimpleNamespace(id=chat_id)

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))


async def _present(monkeypatch, snap: svc.Snapshot, *, operation: str, params: dict) -> dict:
    """Прогнать _present_proposal с заглушками, вернуть params, ушедшие в save_proposal."""
    captured: dict = {}

    async def _fake_state(*_a, **_kw):
        return snap

    async def _fake_save(**kw):
        captured.update(kw)

    monkeypatch.setattr(bm, "read_state", _fake_state)
    monkeypatch.setattr(bm.STORE, "save_proposal", _fake_save)
    monkeypatch.setattr(bm.ux, "proposal_fits", lambda *_a, **_k: True)
    await bm._present_proposal(
        _FakeMsg(),
        chat_id=991001,
        operation=operation,
        params=params,
        summary="s",
        cid="cidfresh1",
    )
    return captured.get("params") or {}


async def test_card_stamps_marker_even_when_snapshot_failed(monkeypatch):
    """Главный инвариант шага: непрочитанный аккаунт даёт черновик с маркером 'unreadable', а не
    черновик без маркера. Иначе гейт не отличил бы его от черновика, созданного до Волны 1.1."""
    saved = await _present(
        monkeypatch,
        svc.Snapshot(svc.SnapshotKind.UNREADABLE, reason="TimeoutError"),
        operation="pause_campaign",
        params={"campaign": "X"},
    )
    assert saved["_freshness"]["kind"] == "unreadable"
    assert "_before" not in saved


async def test_card_attaches_before_when_read_succeeded(monkeypatch):
    saved = await _present(
        monkeypatch,
        svc.Snapshot(svc.SnapshotKind.OK, data={"kind": "status", "before_status": "ENABLED"}),
        operation="pause_campaign",
        params={"campaign": "X"},
    )
    assert saved["_freshness"]["kind"] == "ok"
    assert saved["_before"] == {"kind": "status", "before_status": "ENABLED"}


async def test_saved_template_carries_no_attestation(monkeypatch):
    """§2B: /savetemplate забирает params последнего create_search_campaign. Шаблон живёт месяцами —
    маркер в нём был бы аттестацией чтения, которого для будущего черновика не было вовсе."""
    bm._LAST_SEARCH_PARAMS.pop(991001, None)
    await _present(
        monkeypatch,
        svc.Snapshot(svc.SnapshotKind.NO_DIFF, reason="not_diffable"),
        operation="create_search_campaign",
        params={"campaign_name": "Новая", "final_url": "https://example.com"},
    )
    tpl = bm._LAST_SEARCH_PARAMS.get(991001) or {}
    assert tpl  # шаблон-заготовка действительно записана
    assert not (ATTESTATION_KEYS & set(tpl))
