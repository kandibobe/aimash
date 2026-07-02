"""§20.5: поимённый diff профиля («было→станет») — texts._profile_field_changes / fmt_client_diff.

Менеджер до «да» должен видеть, ЧТО именно добавится/изменится/пропадёт (+«X», −«Y», «Z»: цена
A→B), а не голые счётчики ×N→×M. notes-append рендерится как «+хвост». Офлайн, без БД.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import texts  # noqa: E402
from clients.store import preview_merge  # noqa: E402

_BEFORE = {
    "customer_id": "1000000021",
    "brand": "Kasi Motors",
    "notes": "упор на гарантию",
    "services": [
        {"name": "Седаны", "description": None, "price": "от $4000", "category": "авто"},
        {"name": "Внедорожники", "description": None, "price": "от $7000", "category": "авто"},
    ],
    "contacts": [{"kind": "phone", "value": "+254 712 345 678"}],
    "socials": {},
}


def test_diff_names_added_service_and_price_change():
    patch = {
        "services": [{"name": "седаны", "price": "от $4500"}, {"name": "Пикапы"}],
        "notes": "акция августа",
    }
    after = preview_merge(_BEFORE, patch)
    changes = texts._profile_field_changes(_BEFORE, after, "ru")
    joined = "\n".join(changes)
    assert "+«Пикапы»" in joined  # новая услуга — поимённо
    assert "от $4000" in joined and "от $4500" in joined  # смена цены видна
    assert "заметки: +«акция августа»" in joined  # append-хвост, не перезапись
    assert "−«" not in joined  # мердж ничего не удаляет


def test_diff_names_removed_on_explicit_replace():
    patch = {"services": [{"name": "Только мотоциклы"}], "replace_services": True}
    after = preview_merge(_BEFORE, patch)
    changes = texts._profile_field_changes(_BEFORE, after, "ru")
    joined = "\n".join(changes)
    assert "+«Только мотоциклы»" in joined
    assert "−«Седаны»" in joined and "−«Внедорожники»" in joined  # удаляемое названо явно


def test_diff_caps_long_lists():
    patch = {"services": [{"name": f"Услуга {i}"} for i in range(10)]}
    after = preview_merge(_BEFORE, patch)
    changes = texts._profile_field_changes(_BEFORE, after, "ru")
    joined = "\n".join(changes)
    assert "ещё)" in joined  # (+N ещё) вместо простыни


def test_fmt_client_diff_includes_changes_block():
    patch = {"services": [{"name": "Пикапы"}]}
    after = preview_merge(_BEFORE, patch)
    out = texts.fmt_client_diff(_BEFORE, after, "1000000021", operation="profile_update")
    assert "Изменения" in out and "Пикапы" in out
