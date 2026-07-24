"""§11: видео, присланное ФАЙЛОМ, идёт в визард кампании из видео, а не в txt/csv-ingest.

Telegram шлёт «отправить как файл» (и .mov с iPhone) как document, а не video → фильтр F.video
его не видел, и апдейт доезжал до on_document (fallback) → «пришли txt/csv». §11-флоу был
недостижим для самого частого способа отправки видео.

Проверяем ФАКТИЧЕСКИ зарегистрированные фильтры dp (офлайн-вызов хендлера напрямую регрессию
маршрутизации не поймал бы — ровно та дыра, что и в tests/test_handler_order.py).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.handlers.search_media import _is_video_document  # noqa: E402


def _handler(name):
    return next(h for h in bm.dp.message.handlers if h.callback.__name__ == name)


def _matches(name: str, msg) -> bool:
    """Совпал бы апдейт с фильтрами хендлера (magic-filter resolve, как в диспатче aiogram)."""
    return all(bool(f.callback(msg)) for f in _handler(name).filters)


def _pos(name: str) -> int:
    return [h.callback.__name__ for h in bm.dp.message.handlers].index(name)


def _doc_msg(mime: str = "", file_name: str = "clip.mp4"):
    return SimpleNamespace(
        video=None,
        photo=None,
        text=None,
        document=SimpleNamespace(file_id="f", file_name=file_name, mime_type=mime, file_size=10),
    )


def test_video_document_routes_to_video_handler():
    msg = _doc_msg(mime="video/mp4")
    assert _matches("on_video", msg), (
        "видео файлом не попадает в §11-визард (уедет в txt/csv-ingest)"
    )
    assert _pos("on_video") < _pos("on_document"), (
        "on_video обязан быть ЗАРЕГИСТРИРОВАН раньше on_document — иначе document-ветка съест видео"
    )


def test_video_document_without_mime_matched_by_extension():
    assert _matches("on_video", _doc_msg(mime="", file_name="IMG_0042.MOV"))


def test_plain_video_still_matches():
    msg = SimpleNamespace(video=SimpleNamespace(file_id="v"), document=None, photo=None, text=None)
    assert _matches("on_video", msg)


def test_text_document_still_goes_to_ingest():
    msg = _doc_msg(mime="text/csv", file_name="keys.csv")
    assert not _matches("on_video", msg), "csv-файл не должен уводить в видео-визард"
    assert _matches("on_document", msg)


def test_image_document_not_stolen_by_video_handler():
    """P1-C (§11): изображение-документ по-прежнему обрабатывает on_document (GDN-визард)."""
    msg = _doc_msg(mime="image/png", file_name="creative.png")
    assert not _matches("on_video", msg)
    assert _matches("on_document", msg)


def test_helper_is_none_safe():
    assert _is_video_document(None) is False


def test_video_document_starts_video_wizard():
    """Хендлер на document-видео ведёт себя ровно как на F.video: ждём ссылку на YouTube."""

    class _State:
        def __init__(self):
            self._s = None

        async def get_state(self):
            return self._s

        async def clear(self):
            self._s = None

        async def set_state(self, s):
            self._s = s

    answers: list[str] = []

    class _Msg(SimpleNamespace):
        async def answer(self, t="", **kw):
            answers.append(t)

    msg = _Msg(**vars(_doc_msg(mime="video/quicktime")))
    st = _State()
    asyncio.run(bm.on_video(msg, st))
    assert st._s == bm.VideoWizard.awaiting_link.state
    assert answers and answers[0] == bm.i18n.t("video_received")
