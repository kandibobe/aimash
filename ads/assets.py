"""§11 GDN: подготовка изображений (Pillow) + загрузка медиа-ассетов (AssetService) + временное
хранилище байтов между приёмом фото и подтверждением.

⛔ Замок аккаунта (golden rule #9): upload_image_asset — под ensure_allowed (грузить ассеты можно
ТОЛЬКО в Aimash Draft). Бинарь фото НЕ попадает в proposal.params/логи: между приёмом и «да» он
живёт во ВРЕМЕННЫХ файлах по media_id, а в params идёт только media_id (метаданные). Адаптивному
медийному объявлению нужны два кадра — landscape 1.91:1 и square 1:1 — их КОД режет из одного фото.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from PIL import Image

from ads.client import ensure_allowed

# Целевые размеры (≥ минимумов Google: marketing 600×314, square 300×300) — берём с запасом ×2.
_LANDSCAPE = (1200, 628)  # 1.91:1
_SQUARE = (600, 600)  # 1:1
_MEDIA_DIR = Path(tempfile.gettempdir()) / "aimash_gdn_media"


def _safe_media_id(media_id: str) -> str:
    """media_id идёт в имя файла → защита от path-traversal: только буквы/цифры."""
    if not media_id or not media_id.isalnum():
        raise ValueError("некорректный media_id")
    return media_id


def _crop_resize(img: Image.Image, tw: int, th: int) -> Image.Image:
    """Центр-кроп под целевую пропорцию, затем ресайз в (tw, th)."""
    sw, sh = img.size
    tr, sr = tw / th, sw / sh
    if sr > tr:  # шире нужного → режем по ширине
        nw = int(round(sh * tr))
        x = (sw - nw) // 2
        img = img.crop((x, 0, x + nw, sh))
    elif sr < tr:  # выше нужного → режем по высоте
        nh = int(round(sw / tr))
        y = (sh - nh) // 2
        img = img.crop((0, y, sw, y + nh))
    return img.resize(
        (tw, th), Image.Resampling.LANCZOS
    )  # Resampling — стабильный путь (Pillow≥9.1)


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def prepare_display_images(photo_bytes: bytes) -> tuple[bytes, bytes]:
    """Одно фото → (landscape 1.91:1, square 1:1) JPEG-байты для RDA. ValueError, если не картинка."""
    try:
        img = Image.open(io.BytesIO(photo_bytes))
        img.load()
    except Exception as e:  # noqa: BLE001 — любое не-изображение → понятная ошибка пользователю
        raise ValueError(f"не удалось прочитать изображение: {type(e).__name__}") from e
    return _to_jpeg(_crop_resize(img, *_LANDSCAPE)), _to_jpeg(_crop_resize(img, *_SQUARE))


# ── Временное хранилище байтов (между приёмом фото и confirm; переживает рестарт) ─
def save_pending_media(media_id: str, landscape: bytes, square: bytes) -> None:
    mid = _safe_media_id(media_id)
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (_MEDIA_DIR / f"{mid}.l.jpg").write_bytes(landscape)
    (_MEDIA_DIR / f"{mid}.s.jpg").write_bytes(square)


def load_pending_media(media_id: str) -> tuple[bytes, bytes]:
    mid = _safe_media_id(media_id)
    land = _MEDIA_DIR / f"{mid}.l.jpg"
    sq = _MEDIA_DIR / f"{mid}.s.jpg"
    if not land.exists() or not sq.exists():
        raise FileNotFoundError(f"медиа {mid} не найдено (сессия устарела?)")
    return land.read_bytes(), sq.read_bytes()


def clear_pending_media(media_id: str) -> None:
    try:
        mid = _safe_media_id(media_id)
    except ValueError:
        return
    for suf in (".l.jpg", ".s.jpg"):
        try:
            (_MEDIA_DIR / f"{mid}{suf}").unlink()
        except OSError:
            pass


def clear_pending_media_ids(media_ids) -> None:
    """Пакетно удалить временные кадры по списку media_id (best-effort). §19: чистка осиротевших
    изображений черновика на любом не-исполненном завершении (отмена/supersede/TTL/reject)."""
    for mid in media_ids or []:
        clear_pending_media(str(mid))


# ── AssetService: загрузка image-ассета (SDK) ────────────────────────────────────
def upload_image_asset(client, customer_id: str, image_bytes: bytes, name: str) -> str:
    """Загрузить image-ассет, вернуть resource_name. Замок аккаунта — и тут (golden rule #9)."""
    ensure_allowed(customer_id)
    if not image_bytes:
        raise ValueError("пустые байты изображения")
    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    op.create.name = name
    op.create.type_ = client.enums.AssetTypeEnum.IMAGE
    op.create.image_asset.data = image_bytes
    resp = svc.mutate_assets(customer_id=str(customer_id), operations=[op])
    return resp.results[0].resource_name
