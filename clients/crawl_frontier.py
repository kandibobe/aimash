"""§20.4: фронтир краула — приоритетная очередь + deny-list + нормализация URL.

Зачем: раньше фронтир был плоской FIFO-очередью. На живом сайте (darial.co.jp) бюджет в 50 страниц
съел личный кабинет — из 17 обойденных страниц шесть были `/login/`, `/logout/`, `/dashboard/`,
`/password-reset/`, `/register/`, `/profile-settings/`, а `/about/` (там ровно то, что нужно
владельцу: «over 100 countries», «6,000+ vehicles») доехала последней и в усечённом виде.

Теперь:
- **приоритет** по типу пути: about/services/catalog/price/contacts/team/faq и корень — вперёд,
  блог/новости — в хвост (их много, ценность на страницу низкая);
- **deny-list**: личный кабинет, корзина, wp-admin, фиды, поиск, служебные расширения — вообще не
  запрашиваются (экономия и бюджета, и чужого сервера);
- **дедуп по ключу** без хвостового слэша: `/about` и `/about/` — одна страница. Качаем при этом
  URL «как есть» (не срезая слэш): на WordPress срезанный слэш даёт 301 на КАЖДОЙ странице.
"""

from __future__ import annotations

import heapq
import re
from urllib.parse import parse_qsl, urldefrag, urlencode, urlparse, urlunparse

# Параметры трекинга — шум, из-за которого один и тот же URL выглядит десятком разных.
_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|_ga$|msclkid$|mc_[ec]id$|ref$)")
# Query-параметры, по которым страницу вообще не имеет смысла тянуть (поиск/сортировка/корзина),
# а также фасеты и SSO-обвязка. На живом сайте (darial.co.jp) без них шесть из первых шести
# обойденных страниц были копиями главной вида `/?taxonomy=auto-tags&term=gibrid`, а `/about-us/`
# приезжала ещё и как `?oauthWindow=true&provider=google&um_sso=445` — тот же HTML, другой URL.
_DENY_PARAMS = frozenset(
    {
        "s",
        "q",
        "search",
        "orderby",
        "order",
        "sort",
        "filter",
        "add-to-cart",
        "replytocom",
        "share",
        "taxonomy",
        "term",
        "oauthwindow",
        "um_sso",
        "provider",
        "currency",
    }
)
# Сегменты пути личного кабинета и служебки. Совпадение по СЕГМЕНТУ (не подстрокой), чтобы
# `/services/` не поймался на «s» и `/blogin/` — на «login».
_DENY_SEGMENTS = frozenset(
    {
        "login",
        "logout",
        "signin",
        "sign-in",
        "signout",
        "sign-out",
        "register",
        "signup",
        "sign-up",
        "password-reset",
        "reset-password",
        "forgot-password",
        "lost-password",
        "my-account",
        "account",
        "dashboard",
        "profile",
        "profile-settings",
        "settings",
        "cart",
        "checkout",
        "basket",
        "wishlist",
        "compare",
        "wp-admin",
        "wp-login.php",
        "wp-json",
        "xmlrpc.php",
        "feed",
        "rss",
        "comments",
        "trackback",
        "cdn-cgi",
        "author",
        "print",
    }
)
# Расширения, которые точно не HTML: отвергаем ДО запроса (content-type-гард отверг бы, но уже
# потратив соединение и такт вежливой паузы).
_DENY_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
    ".css",
    ".js",
    ".json",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".exe",
    ".dmg",
    ".apk",
)

# Приоритет обхода: меньше — раньше. Ключевое отличие от FIFO: страница «о компании» уходит в
# начало очереди, даже если ссылка на неё нашлась на 40-й обойденной странице.
PRIO_TOP = 0  # корень
PRIO_HIGH = 1  # about / services / contacts / price / team / faq — суть профиля
PRIO_NORMAL = 3  # обычные страницы
PRIO_LOW = 5  # блог/новости/теги/архивы — много, ценность на страницу низкая

_HIGH_HINTS = (
    "about",
    "company",
    "o-nas",
    "о-нас",
    "team",
    "staff",
    "people",
    "management",
    "service",
    "услуг",
    "product",
    "catalog",
    "catalogue",
    "shop",
    "price",
    "pricing",
    "прайс",
    "цен",
    "tariff",
    "contact",
    "контакт",
    "faq",
    "how-it-works",
    "delivery",
    "shipping",
    "payment",
    "warranty",
    "guarantee",
    "reviews",
    "testimonial",
)
# Низкий приоритет — по СЕГМЕНТУ, не подстрокой: иначе «/homepage» ловится на «page», а
# «/postal-services» — на «post».
_LOW_SEGMENTS = frozenset(
    {
        "blog",
        "news",
        "article",
        "articles",
        "tag",
        "tags",
        "category",
        "categories",
        "archive",
        "archives",
        "page",
        "post",
        "posts",
    }
)


def normalize(url: str) -> str:
    """URL для запроса: без фрагмента и трекинг-параметров. Хвостовой слэш НЕ трогаем (см. модуль)."""
    u, _frag = urldefrag((url or "").strip())
    p = urlparse(u)
    if p.query:
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _is_noise(k)]
        u = urlunparse(p._replace(query=urlencode(kept)))
    return u


def _is_noise(key: str) -> bool:
    return bool(_TRACKING_PARAMS.match((key or "").lower()))


def dedup_key(url: str) -> str:
    """Ключ дедупликации: схема+хост (без www) + путь без хвостового слэша + значимый query."""
    p = urlparse(normalize(url))
    host = (p.netloc or "").lower().split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "/").rstrip("/") or "/"
    return f"{p.scheme}://{host}{path}" + (f"?{p.query}" if p.query else "")


def is_denied(url: str) -> bool:
    """Личный кабинет / корзина / служебка / не-HTML — не запрашиваем вовсе."""
    p = urlparse(url or "")
    path = (p.path or "/").lower()
    if path.endswith(_DENY_EXTS):
        return True
    segments = {seg for seg in path.split("/") if seg}
    if segments & _DENY_SEGMENTS:
        return True
    keys = {k.lower() for k, _v in parse_qsl(p.query or "", keep_blank_values=True)}
    return bool(keys & _DENY_PARAMS)


def priority(url: str) -> int:
    """Чем меньше число, тем раньше страница будет обойдена."""
    p = urlparse(url or "")
    path = (p.path or "/").lower()
    if path in ("", "/"):
        # Корень С query — это фильтр/фасет каталога, а не главная: тот же HTML под другим URL.
        # Без этого шесть копий главной выбивались вперёд настоящих `/about/` и `/services/`.
        return PRIO_TOP if not p.query else PRIO_LOW
    if any(h in path for h in _HIGH_HINTS):
        return PRIO_HIGH
    if {seg for seg in path.split("/") if seg} & _LOW_SEGMENTS:
        return PRIO_LOW
    # Глубокие карточки (много сегментов) — обычно однотипный каталог: чуть позже обычных страниц.
    return PRIO_NORMAL if path.count("/") <= 2 else PRIO_NORMAL + 1


class Frontier:
    """Приоритетная очередь URL с дедупом и deny-list. Внутри одного приоритета — FIFO (стабильно)."""

    def __init__(self, *, max_depth: int = 3) -> None:
        self.max_depth = max_depth
        self._heap: list[tuple[int, int, str, int]] = []  # (prio, seq, url, depth)
        self._seen: set[str] = set()
        self._seq = 0
        self.denied = 0  # сколько ссылок отсеяно deny-list (для диагностики)

    def __len__(self) -> int:
        return len(self._heap)

    def seen_count(self) -> int:
        return len(self._seen)

    def push(self, url: str, depth: int, *, prio: int | None = None) -> bool:
        """Добавить URL. False — если дубль, запрещён или глубже max_depth."""
        if depth > self.max_depth:
            return False
        u = normalize(url)
        if not u:
            return False
        key = dedup_key(u)
        if key in self._seen:
            return False
        if is_denied(u):
            self._seen.add(key)  # чтобы не считать его снова на каждой странице
            self.denied += 1
            return False
        self._seen.add(key)
        self._seq += 1
        heapq.heappush(self._heap, (priority(u) if prio is None else prio, self._seq, u, depth))
        return True

    def pop(self) -> tuple[str, int] | None:
        if not self._heap:
            return None
        _prio, _seq, url, depth = heapq.heappop(self._heap)
        return url, depth
