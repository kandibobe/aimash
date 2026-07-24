"""Генератор брендовых изображений Aimash (Pillow).

Создаёт два PNG в `bot/assets/`:
  • welcome.png (1280×720) — баннер-подпись к /start (отправляется ботом);
  • logo.png    (640×640)  — квадратный логотип/аватар (загрузить в @BotFather как фото профиля).

Запуск (одноразово, на машине со шрифтами — например Windows):
    python scripts/make_welcome_image.py

Рантайму бота шрифты НЕ нужны — он читает готовые PNG (закоммичены в репозиторий).
Перегенерировать — после правок дизайна здесь. Фирменный мотив: «рост рекламы + подтверждение»
(растущие столбцы + зелёная галочка) — отражает суть бота: меняем только после «да».
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ── Палитра ──────────────────────────────────────────────────────────────────────
BG_TOP = (10, 19, 49)  # #0A1331 — глубокий навигейт
BG_BOTTOM = (22, 37, 92)  # #16255C — индиго
WHITE = (255, 255, 255)
SUBTLE = (176, 187, 226)  # приглушённый сиренево-серый для подзаголовков
INDIGO = (91, 124, 250)  # #5B7CFA — акцент
# Цвета Google Ads — тонкий акцент-штрих.
G_BLUE = (66, 133, 244)
G_RED = (234, 67, 53)
G_YELLOW = (251, 188, 5)
G_GREEN = (52, 168, 83)

_FONT_DIR = Path("C:/Windows/Fonts")
_OUT_DIR = Path(__file__).resolve().parent.parent / "bot" / "assets"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Грузим Segoe UI с фолбэком на дефолт Pillow (чтобы скрипт не падал вне Windows)."""
    for candidate in (_FONT_DIR / name, Path(name)):
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    """Вертикальный градиент top→bottom построчно (h строк — дёшево)."""
    w, h = size
    base = Image.new("RGB", (1, h))
    px = base.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return base.resize((w, h))


def _glow(size: tuple[int, int], center: tuple[int, int], radius: int, color: tuple, alpha: int):
    """Мягкое радиальное свечение (эллипс + сильный блюр) поверх фона — объём без плоскости."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))
    return layer.filter(ImageFilter.GaussianBlur(radius // 2))


def _logo_mark(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], img: Image.Image) -> None:
    """Фирменный знак в квадрате box: бейдж-плашка с растущими столбцами + зелёная галочка.

    Читается как «рост рекламных показателей + подтверждение» — ядро продукта (см. шапку модуля).
    """
    x0, y0, x1, y1 = box
    s = x1 - x0
    r = int(s * 0.22)  # радиус скругления плашки

    # Плашка с лёгким градиентом (синий → индиго) через маску скруглённого прямоугольника.
    badge = _vertical_gradient((s, s), G_BLUE, INDIGO)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=r, fill=255)
    img.paste(badge, (x0, y0), mask)

    # Растущие столбцы (3 шт.) внутри плашки — белые, полупрозрачные.
    pad = int(s * 0.22)
    bw = int(s * 0.13)  # ширина столбца
    gap = int((s - 2 * pad - 3 * bw) / 2)
    base_y = y1 - pad
    heights = [0.30, 0.52, 0.78]  # доли высоты «рабочей зоны»
    work_h = s - 2 * pad
    bx = x0 + pad
    for hfrac in heights:
        bh = int(work_h * hfrac)
        draw.rounded_rectangle(
            (bx, base_y - bh, bx + bw, base_y), radius=int(bw * 0.35), fill=(255, 255, 255, 235)
        )
        bx += bw + gap

    # Зелёный кружок-галочка в правом-верхнем углу плашки (мотив «подтверждено»).
    cd = int(s * 0.40)
    cx0 = x1 - cd + int(s * 0.06)
    cy0 = y0 - int(s * 0.06)
    draw.ellipse(
        (cx0, cy0, cx0 + cd, cy0 + cd), fill=G_GREEN, outline=BG_TOP, width=max(3, s // 60)
    )
    # Галочка внутри кружка.
    ccx, ccy = cx0 + cd / 2, cy0 + cd / 2
    lw = max(4, cd // 9)
    draw.line(
        [
            (ccx - cd * 0.20, ccy + cd * 0.02),
            (ccx - cd * 0.04, ccy + cd * 0.18),
            (ccx + cd * 0.24, ccy - cd * 0.18),
        ],
        fill=WHITE,
        width=lw,
        joint="curve",
    )


def _google_stripe(draw: ImageDraw.ImageDraw, x: int, y: int, seg_w: int, h: int, gap: int) -> None:
    """Короткий 4-цветный штрих Google (синий/красный/жёлтый/зелёный) — тонкий бренд-акцент."""
    for i, col in enumerate((G_BLUE, G_RED, G_YELLOW, G_GREEN)):
        sx = x + i * (seg_w + gap)
        draw.rounded_rectangle((sx, y, sx + seg_w, y + h), radius=h // 2, fill=col)


_CHIP_PAD_X, _CHIP_PAD_Y = 28, 16


def _chip_size(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont
) -> tuple[int, int]:
    tb = draw.textbbox((0, 0), text, font=font)
    return tb[2] - tb[0] + _CHIP_PAD_X * 2, tb[3] - tb[1] + _CHIP_PAD_Y * 2


def _chip(od: ImageDraw.ImageDraw, x: int, y: int, text: str, font: ImageFont.FreeTypeFont) -> None:
    """Скруглённый «чип» с текстом фичи (без эмодзи — Segoe UI ч/б их не содержит).

    Рисуется на ОТДЕЛЬНОМ RGBA-слое (od), который потом alpha_composite-ится на фон: прямой draw
    по RGBA-картинке альфу не блендит, а теряет при convert('RGB') → плашки выходят непрозрачными.
    """
    w, h = _chip_size(od, text, font)
    od.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(255, 255, 255, 28))
    od.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, outline=(255, 255, 255, 70), width=2)
    od.text((x + _CHIP_PAD_X, y + h / 2), text, font=font, fill=(214, 222, 250), anchor="lm")


def build_welcome(path: Path) -> None:
    """Баннер 1280×720 — отправляется как фото к /start."""
    W, H = 1280, 720
    img = _vertical_gradient((W, H), BG_TOP, BG_BOTTOM).convert("RGBA")
    # Объём: два мягких свечения.
    img.alpha_composite(_glow((W, H), (W - 220, 160), 360, INDIGO, 90))
    img.alpha_composite(_glow((W, H), (180, H - 120), 320, G_BLUE, 60))
    draw = ImageDraw.Draw(img, "RGBA")

    # Логознак + вордмарк.
    mark = 150
    mx, my = 96, 92
    _logo_mark(draw, (mx, my, mx + mark, my + mark), img)
    draw = ImageDraw.Draw(img, "RGBA")  # пересоздаём после paste плашки

    title_f = _font("segoeuib.ttf", 132)
    draw.text((mx + mark + 40, my + mark / 2), "Aimash", font=title_f, fill=WHITE, anchor="lm")

    # Подзаголовок.
    sub_f = _font("segoeuisl.ttf", 44)
    draw.text(
        (mx, my + mark + 56),
        "Google Ads под управлением ИИ — прямо в Telegram",
        font=sub_f,
        fill=SUBTLE,
        anchor="lm",
    )

    # Google-штрих под подзаголовком.
    _google_stripe(draw, mx + 2, my + mark + 96, seg_w=58, h=10, gap=12)

    # Нижняя подпись-призыв (opaque-текст — можно прямо на img).
    foot_f = _font("segoeuil.ttf", 32)
    draw.text(
        (mx, H - 56),
        "Напиши обычным текстом или нажми /start",
        font=foot_f,
        fill=(226, 232, 252),
        anchor="lm",
    )

    # Полупрозрачные элементы (плашка принципа + чипы) — на ОТДЕЛЬНОМ RGBA-слое, потом composite.
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay, "RGBA")

    # Ключевой принцип: «… только после твоего «да» ✅» — плашка-стекло по центру.
    panel_y, panel_h = 388, 96
    panel = (mx, panel_y, W - mx, panel_y + panel_h)
    od.rounded_rectangle(panel, radius=24, fill=(255, 255, 255, 26))
    od.rounded_rectangle(panel, radius=24, outline=(255, 255, 255, 60), width=2)
    pf = _font("segoeuib.ttf", 36)  # bold существует в системе (semibold — нет)
    cy = panel_y + panel_h / 2
    head = "Любое изменение — только после твоего "
    od.text((mx + 36, cy), head, font=pf, fill=WHITE, anchor="lm")  # текст opaque поверх плашки
    da_x = mx + 36 + od.textbbox((0, 0), head, font=pf)[2]
    od.text((da_x, cy), "«да»", font=pf, fill=G_YELLOW, anchor="lm")
    chk = da_x + od.textbbox((0, 0), "«да»", font=pf)[2] + 22
    rcd = 44
    od.ellipse((chk, cy - rcd / 2, chk + rcd, cy + rcd / 2), fill=G_GREEN)
    od.line(
        [
            (chk + rcd * 0.26, cy + rcd * 0.02),
            (chk + rcd * 0.43, cy + rcd * 0.20),
            (chk + rcd * 0.76, cy - rcd * 0.22),
        ],
        fill=WHITE,
        width=6,
        joint="curve",
    )

    # Ряд чипов-возможностей (текст без эмодзи; перенос на 2-й ряд ДО отрисовки).
    chip_f = _font("segoeui.ttf", 28)
    cx, cy2 = mx, 500
    for label in (
        "Статистика и отчёты",
        "Тексты объявлений",
        "Подбор ключевых слов",
        "Бюджет и ставки",
    ):
        w, _h = _chip_size(od, label, chip_f)
        if cx > mx and cx + w > W - mx:
            cx = mx
            cy2 += _h + 16
        _chip(od, cx, cy2, label, chip_f)
        cx += w + 16

    img.alpha_composite(overlay)
    img.convert("RGB").save(path, "PNG")
    print(f"[ok] {path} ({W}x{H})")


def build_logo(path: Path) -> None:
    """Квадратный логотип/аватар 640×640 — для фото профиля бота в @BotFather."""
    S = 640
    img = _vertical_gradient((S, S), BG_TOP, BG_BOTTOM).convert("RGBA")
    img.alpha_composite(_glow((S, S), (S - 120, 120), 240, INDIGO, 110))
    draw = ImageDraw.Draw(img, "RGBA")

    mark = 300
    off = (S - mark) // 2 - 36  # чуть выше центра — под вордмарк снизу
    _logo_mark(draw, (off + 30, off, off + 30 + mark, off + mark), img)
    draw = ImageDraw.Draw(img, "RGBA")

    name_f = _font("segoeuib.ttf", 92)
    draw.text((S / 2, off + mark + 70), "Aimash", font=name_f, fill=WHITE, anchor="mm")
    _google_stripe(draw, S // 2 - 116, off + mark + 132, seg_w=46, h=9, gap=12)

    img.convert("RGB").save(path, "PNG")
    print(f"[ok] {path} ({S}x{S})")


def main() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_welcome(_OUT_DIR / "welcome.png")
    build_logo(_OUT_DIR / "logo.png")


if __name__ == "__main__":
    main()
