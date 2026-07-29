"""Единственное место в `hermes_ops`, где разрешён POST — и то по одному вшитому пути.

Дашборд гейтит `/api/*` двумя разными механизмами, и активен ровно один — по адресу бинда
(`hermes_cli/web_server.py:400`, `should_require_auth`):

* **loopback-бинд** (сегодня): работает СТАРЫЙ гейт `auth_middleware` — эфемерный `_SESSION_TOKEN`,
  который дашборд впечатывает в HTML главной страницы (`web_server.py:17924`) и ждёт обратно в
  заголовке `X-Hermes-Session-Token`. Куки провайдера пароля в этом режиме не смотрит НИКТО:
  `gated_auth_middleware` при `auth_required=False` — сквозной проход (`web_server.py:589`).
* **не-loopback бинд**: включается `gated_auth_middleware` — вход по паролю (или OAuth), сессия в
  куках, а `_SESSION_TOKEN` в HTML уже НЕ попадает.

Поэтому здесь два пути входа, и выбирается он не конфигом, а замером: тянем `/`, нашли токен —
режим loopback; не нашли (200 без токена или 302 на `/login`) — режим гейта, логинимся паролем.

**Почему POST вообще допущен.** Граница слоя — «инструмент не может ничего изменить на VPS». Один
POST на `/auth/password-login` её не двигает: путь — литерал модуля, вызывающий его не задаёт и
подставить свой не может; тело — только учётные данные из окружения; ответ — куки. Гард в
`tests/test_hermes_ops_surface.py` разрешает `.post(` ровно в этом файле и ровно с этим литералом.

Секреты (токен сессии, пароль) живут в памяти процесса, наружу вычищаются: `client.redact_deep`
получает их значения и заменяет на `REDACTED` (правило 5).
"""

from __future__ import annotations

import os
import re
from typing import Any, Final

import httpx

#: Путь входа по паролю. ЛИТЕРАЛ — не параметр: вызывающий не выбирает, куда уйдёт POST.
LOGIN_PATH: Final[str] = "/auth/password-login"
#: Провайдер из комплекта Hermes (`plugins/dashboard_auth/basic`, `name = "basic"`).
LOGIN_PROVIDER: Final[str] = "basic"
#: Главная страница SPA — из неё в loopback-режиме читается токен сессии.
ROOT_PATH: Final[str] = "/"
SESSION_HEADER: Final[str] = "X-Hermes-Session-Token"

ENV_USERNAME: Final[str] = "HERMES_DASHBOARD_USERNAME"
ENV_PASSWORD: Final[str] = "HERMES_DASHBOARD_PASSWORD"

# `_SESSION_TOKEN = secrets.token_urlsafe(32)` → 43 символа base64url. Диапазон с запасом.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r'window\.__HERMES_SESSION_TOKEN__\s*=\s*"([A-Za-z0-9_\-]{16,512})"'
)


class DashboardAuth:
    """Состояние входа: заголовок с токеном ИЛИ куки сессии, плюс причина отказа для диагностики."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._cookies: httpx.Cookies = httpx.Cookies()
        self._mode: str = "не установлен"
        self._last_error: str | None = None

    # ── состояние ───────────────────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return bool(self._token) or bool(list(self._cookies.jar))

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def headers(self) -> dict[str, str]:
        return {SESSION_HEADER: self._token} if self._token else {}

    @property
    def cookies(self) -> httpx.Cookies:
        return self._cookies

    def secret_values(self) -> tuple[str, ...]:
        """Что вычищать из любого ответа наружу: сам токен и пароль (правило 5)."""
        return tuple(v for v in (self._token, os.getenv(ENV_PASSWORD)) if v)

    def diagnosis(self) -> str:
        """Человекочитаемая причина, БЕЗ значений секретов — идёт в error-конверт инструмента."""
        if self.ready:
            return f"вход выполнен ({self._mode})"
        return self._last_error or "вход не выполнялся"

    def forget(self) -> None:
        self._token = None
        self._cookies = httpx.Cookies()
        self._mode = "не установлен"

    # ── вход ────────────────────────────────────────────────────────────────────────────

    async def ensure(self, http: httpx.AsyncClient) -> None:
        """Best-effort вход. Провал НЕ роняет вызов: публичные ручки (`/api/status`) работают и без
        сессии, а на закрытых 401 придёт от самого дашборда — с причиной из `diagnosis()`."""
        if self.ready:
            return
        try:
            await self.refresh(http)
        except Exception as exc:  # noqa: BLE001 — причина уходит в конверт, не в исключение
            self._last_error = f"{type(exc).__name__}: {exc}"[:300]

    async def ensure_refreshed(self, http: httpx.AsyncClient) -> None:
        """То же best-effort, но принудительно: вызывается после 401 (токен мог протухнуть)."""
        self.forget()
        await self.ensure(http)

    async def refresh(self, http: httpx.AsyncClient) -> str:
        """Переустановить сессию. Возвращает режим; при неудаче — `RuntimeError` без секретов."""
        self.forget()
        token = await self._read_injected_token(http)
        if token:
            self._token = token
            self._mode = "loopback-токен"
            self._last_error = None
            return self._mode
        await self._password_login(http)
        self._mode = "пароль"
        self._last_error = None
        return self._mode

    async def _read_injected_token(self, http: httpx.AsyncClient) -> str | None:
        """GET `/` → токен из bootstrap-скрипта SPA. Нет токена ⇒ дашборд в режиме гейта."""
        response = await http.get(ROOT_PATH)
        if response.status_code != 200:
            return None
        match = _TOKEN_RE.search(response.text)
        return match.group(1) if match else None

    async def _password_login(self, http: httpx.AsyncClient) -> None:
        username = (os.getenv(ENV_USERNAME) or "").strip()
        password = os.getenv(ENV_PASSWORD) or ""
        if not username or not password:
            raise RuntimeError(
                f"дашборд в режиме гейта (токен в HTML не отдаётся), а {ENV_USERNAME}/"
                f"{ENV_PASSWORD} не заданы — отказываю (правило 10). Учётные данные лежат в "
                ".claude/settings.local.json и пробрасываются через .mcp.json."
            )
        # ЕДИНСТВЕННЫЙ POST во всём пакете. Путь — литерал LOGIN_PATH, телом идут только креды.
        response = await http.post(
            LOGIN_PATH,
            json={
                "provider": LOGIN_PROVIDER,
                "username": username,
                "password": password,
                "next": "/",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"вход по паролю отклонён: HTTP {response.status_code} "
                f"(провайдер {LOGIN_PROVIDER!r}, пользователь {username!r}). "
                "404 — провайдер не поднят на сервере, 401 — не те учётные данные, "
                "429 — лимит попыток (10 в минуту на IP)."
            )
        self._cookies.update(response.cookies)
        if not list(self._cookies.jar):
            raise RuntimeError("вход вернул 200, но кук сессии в ответе нет — считаю отказом")


#: Общая на процесс сессия: клиент строится на каждый вызов инструмента, вход — нет.
SESSION: Final[DashboardAuth] = DashboardAuth()


def describe_session() -> dict[str, Any]:
    """Состояние входа для диагностики. Без значений — только режим и готовность."""
    return {"ready": SESSION.ready, "mode": SESSION.mode, "detail": SESSION.diagnosis()}
