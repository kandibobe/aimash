"""Транспорт к дашборду Hermes — физически неспособный писать.

Инвариант тот же, что И4 держит для мутаций Google Ads: опасная операция не «не вызывается», а **не
существует**. У `HermesReadClient` нет и не может появиться `post`/`put`/`patch`/`delete` — есть
единственный метод `get`, и он отказывает на любом пути вне `allowlist.READ_ENDPOINTS`. Проверка
живёт в транспорте, а не в инструментах: инструмент, забывший про allow-list, всё равно не выйдет
в сеть.

Три рубежа поверх этого:
  • путь берётся из ШАБЛОНА в allow-list, а подстановки URL-квотятся с `safe=""` — `{session_id}`
    вида `../../api/env/reveal` превращается в `..%2F..%2Fapi%2Fenv%2Freveal` и остаётся одним
    сегментом пути (обхода allow-list через параметр нет);
  • редиректы выключены (`follow_redirects=False`): 302 с дашборда увёл бы GET на путь, который
    allow-list никогда не одобрял;
  • всё, что вернулось, проходит `core.logging.redact_text` по каждой строке — `/api/config` и
    `/api/logs` могут нести токен (правило 5, три рубежа редакции — `docs/SECURITY.md`).

База берётся из `HERMES_DASHBOARD_URL` и **без дефолта**: не задан — клиент не строится (правило 10,
fail-closed). Зашитый дефолт означал бы «молча постучались не туда, куда думали».

Вход в дашборд (токен сессии или пароль) целиком вынесен в `hermes_ops.auth` — там же живёт и
единственный на весь пакет POST. Здесь только GET, и это проверяется разбором исходника в тестах.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

from core.logging import REDACTED, redact_text
from hermes_ops.allowlist import READ_ENDPOINTS
from hermes_ops.auth import SESSION, DashboardAuth

ENV_BASE_URL: Final[str] = "HERMES_DASHBOARD_URL"
ENV_TIMEOUT: Final[str] = "HERMES_DASHBOARD_TIMEOUT"
DEFAULT_TIMEOUT_S: Final[float] = 15.0


def resolve_base_url() -> str:
    """База дашборда из окружения. Пусто/не-http(s) ⇒ `RuntimeError` (fail-closed, правило 10)."""
    raw = (os.getenv(ENV_BASE_URL) or "").strip().rstrip("/")
    if not raw:
        raise RuntimeError(
            f"{ENV_BASE_URL} не задан — куда стучаться, неизвестно, отказываю (правило 10). "
            "Задай tailnet-URL дашборда в .claude/settings.local.json "
            "(напр. https://<хост>.ts.net). Дефолта здесь нет намеренно."
        )
    scheme = urlsplit(raw).scheme
    if scheme not in ("http", "https"):
        raise RuntimeError(
            f"{ENV_BASE_URL}: ожидается http/https, получено {scheme!r} — отказываю."
        )
    return raw


def resolve_timeout() -> float:
    """Таймаут запроса. Кривое значение — не повод падать: берём дефолт (сеть, не денежный путь)."""
    try:
        value = float(os.getenv(ENV_TIMEOUT, "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def redact_deep(obj: Any, extra: Sequence[str] = ()) -> Any:
    """Рекурсивная редакция строк в ответе с сохранением структуры.

    Редактировать целиком сериализованный JSON нельзя: `redact_text` может изменить длину/содержимое
    так, что обратный `json.loads` упадёт. Поэтому идём по структуре и чистим листья-строки — форма
    ответа сохраняется, секрет наружу не выходит. Ключи словарей тоже чистим: имя переменной вида
    `OPENROUTER_API_KEY=sk-...` встречается в ключах конфига.

    `extra` — значения, которые `redact_text` по шаблону не узнает: токен сессии дашборда и пароль.
    Дашборд возвращает свой же токен как минимум в `/api/config`, так что это не теория.
    """
    needles = tuple(s for s in extra if s and len(s) >= 8)

    def clean(text: str) -> str:
        for needle in needles:
            text = text.replace(needle, REDACTED)
        return redact_text(text)

    if isinstance(obj, str):
        return clean(obj)
    if isinstance(obj, dict):
        return {redact_deep(k, extra): redact_deep(v, extra) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_deep(x, extra) for x in obj]
    return obj


class HermesReadClient:
    """GET-only клиент дашборда. Методов записи нет — это и есть граница, а не соглашение."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        auth: DashboardAuth | None = None,
    ) -> None:
        self._base_url = base_url if base_url is not None else resolve_base_url()
        self._timeout = timeout if timeout is not None else resolve_timeout()
        # Сессия общая на процесс: клиент строится на каждый вызов, вход — один раз.
        self._auth = auth if auth is not None else SESSION

    @property
    def base_url(self) -> str:
        return self._base_url

    def build_path(self, template: str, path_params: dict[str, Any] | None = None) -> str:
        """Шаблон + подстановки → путь. Шаблон вне allow-list ⇒ `RuntimeError` (fail-closed).

        Отдельный от `get` метод, чтобы гард проверялся тестом без выхода в сеть.
        """
        if template not in READ_ENDPOINTS:
            raise RuntimeError(
                f"путь {template!r} отсутствует в allow-list (hermes_ops.allowlist.READ_ENDPOINTS) — "
                "отказываю. Поверхность дашборда закрыта по построению: его REST умеет писать файлы "
                "под root и раскрывать секреты, а сессия для этого REST у нас уже есть."
            )
        if not path_params:
            return template
        # safe="" — квотим и слэши: подстановка обязана остаться ОДНИМ сегментом пути.
        return template.format(**{k: quote(str(v), safe="") for k, v in path_params.items()})

    async def get(
        self,
        template: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        """GET по разрешённому пути → распарсенный и редактированный ответ.

        `query` чистится от `None` (httpx иначе шлёт пустые параметры, а дашборд на части ручек
        различает «не передан» и «пустой»).

        Отказ 401/403 — не обязательно «нет прав»: токен сессии дашборда эфемерный и меняется при
        каждом его рестарте. Поэтому один повтор после переустановки сессии; второй 401 отдаём как
        отказ с причиной из `auth.diagnosis()` (без значений секретов).
        """
        path = self.build_path(template, path_params)
        params = {k: v for k, v in (query or {}).items() if v is not None}
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            follow_redirects=False,  # редирект увёл бы GET мимо allow-list
            cookies=self._auth.cookies,  # куки прошлого входа; ответы httpx докладывает сам
        ) as http:
            await self._auth.ensure(http)
            response = await http.get(path, params=params, headers=self._auth.headers)
            if response.status_code in (401, 403):
                await self._auth.ensure_refreshed(http)
                response = await http.get(path, params=params, headers=self._auth.headers)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"дашборд отказал: HTTP {response.status_code} на {template}. "
                f"Состояние входа: {self._auth.diagnosis()}"
            )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            # Не JSON (часть ручек отдаёт текст) — возвращаем текст, тоже редактированный.
            payload = response.text
        return redact_deep(payload, self._auth.secret_values())
