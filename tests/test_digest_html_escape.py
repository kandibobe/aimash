"""B14: имя аккаунта из Google Ads в HTML-дайджестах экранируется — иначе «<»/«&» в имени
молча ломают parse_mode=HTML и Telegram не доставляет сообщение (thr_tune_offer, recommendations).
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scheduler.jobs as jobs  # noqa: E402


def test_digest_account_label_escapes_html(monkeypatch):
    # имя аккаунта с «<»/«&» (враждебное/невинное) → экранировано, id остаётся
    meta = {"555": SimpleNamespace(name="A & B <Ltd>")}
    monkeypatch.setattr("ads.client.discovered_read_children_meta", lambda: meta, raising=False)
    label = jobs._digest_account_label("555")
    assert "<Ltd>" not in label and "&lt;Ltd&gt;" in label
    assert "&amp;" in label  # «&» экранирован
    assert "555" in label


def test_digest_account_label_plain_id_safe():
    # нет meta → голый id (числа не нуждаются в escape, но проходят через esc идемпотентно)
    label = jobs._digest_account_label("7753643025")
    assert label == "7753643025"


def test_html_sends_in_jobs_use_escaped_account_label():
    """Класс-гард B14: обе per-account HTML-рассылки берут имя через _digest_account_label
    (единый экранирующий источник), а не сырое имя аккаунта."""
    for name in ("run_recommendations_digest", "run_threshold_tuning"):
        src = inspect.getsource(getattr(jobs, name))
        if "parse_mode" in src and "HTML" in src:
            assert "_digest_account_label" in src, (
                f"{name} шлёт HTML с именем аккаунта — оно должно идти через _digest_account_label"
            )
    # сам ярлык-билдер обязан экранировать (esc)
    assert re.search(r"esc\(", inspect.getsource(jobs._digest_account_label))
