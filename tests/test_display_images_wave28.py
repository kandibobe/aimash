"""§11 GDN: подготовка изображений — ориентация по EXIF и прозрачность (волна 2.8).

1. EXIF Orientation не применялся: телефон пишет кадр «как снято» + флаг поворота; просмотрщики
   (и Telegram) флаг учитывают, Google — НЕТ → портретный креатив уезжал в объявление боком.
2. `convert("RGB")` кладёт альфу на ЧЁРНЫЙ фон → PNG-логотип с прозрачностью попадал в объявление
   на чёрном прямоугольнике.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.assets import _flatten, prepare_display_images  # noqa: E402


def _jpeg_size(raw: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(raw)).size


def test_exif_orientation_is_applied():
    """Кадр 400×200 с флагом «повернуть на 90°» = по смыслу портрет 200×400: после transpose
    центр-кроп в landscape режет ВЫСОТУ. Без transpose кроп шёл бы по «сырым» пикселям."""
    img = Image.new("RGB", (400, 200), "white")
    for x in range(200, 400):  # правая половина чёрная — маркер, куда «смотрит» кадр
        for y in range(200):
            img.putpixel((x, y), (0, 0, 0))
    exif = img.getexif()
    exif[274] = 6  # Orientation = rotate 270° CW (обычный портрет с камеры)
    raw = io.BytesIO()
    img.save(raw, format="JPEG", exif=exif)

    land, sq = prepare_display_images(raw.getvalue())
    assert _jpeg_size(land) == (1200, 628) and _jpeg_size(sq) == (600, 600)

    rotated = Image.open(io.BytesIO(land)).convert("L")

    def _avg(box) -> float:
        return rotated.crop(box).resize((1, 1)).getpixel((0, 0))

    # Orientation=6 = повернуть на 90° CW: бывшая ПРАВАЯ (тёмная) половина уезжает ВНИЗ.
    # Кадр становится разделён по ГОРИЗОНТАЛИ: верх светлый, низ тёмный.
    top, bottom = _avg((0, 0, 1200, 60)), _avg((0, 568, 1200, 628))
    left, right = _avg((0, 0, 60, 628)), _avg((1140, 0, 1200, 628))
    assert bottom < top, "EXIF Orientation не применён — креатив уедет в объявление боком"
    # Без transpose тёмной была бы ПРАВАЯ половина (кадр «как в файле»), а не нижняя.
    assert abs(left - right) < 40, "кадр не повёрнут: тёмная зона осталась справа"


def test_transparent_png_gets_white_background():
    png = Image.new("RGBA", (800, 800), (0, 0, 0, 0))  # полностью прозрачный
    raw = io.BytesIO()
    png.save(raw, format="PNG")

    land, _sq = prepare_display_images(raw.getvalue())
    out = Image.open(io.BytesIO(land)).convert("RGB")
    r, g, b = out.getpixel((600, 300))
    assert min(r, g, b) > 240, f"прозрачность легла на чёрный фон ({r},{g},{b})"


def test_flatten_keeps_opaque_pixels():
    src = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
    assert _flatten(src).getpixel((5, 5)) == (255, 0, 0)


def test_palette_png_with_transparency_flattened():
    """P-режим с transparency в info — тот же чёрный фон, если не развернуть в RGBA."""
    rgba = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    pal = rgba.convert("P", palette=Image.Palette.ADAPTIVE)
    pal.info["transparency"] = 0
    out = _flatten(pal)
    assert out.mode == "RGB"
