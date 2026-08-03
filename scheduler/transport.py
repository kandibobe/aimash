"""Minimal Telegram Bot API transport for background scheduler delivery.

The scheduler needs outbound messages and documents, not aiogram's dispatcher,
FSM, handlers, or model layer.  Keeping this adapter on ``httpx`` lets the
Hermes runtime remain independent from the removed legacy bot architecture.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

_RETRY_AFTER_MAX_ATTEMPTS = 3


class TelegramRetryAfter(RuntimeError):
    """Telegram Bot API 429 response with a server-provided retry delay."""

    def __init__(self, retry_after: float, message: str = "flood control") -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramBot:
    """Small async client exposing only the scheduler's required Bot API calls."""

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def _request(self, method: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.post(f"{self._base_url}/{method}", **kwargs)
        payload = response.json()
        if response.status_code == 429:
            delay = float(payload.get("parameters", {}).get("retry_after", 1.0))
            raise TelegramRetryAfter(delay, str(payload.get("description", "flood control")))
        response.raise_for_status()
        if not payload.get("ok"):
            raise RuntimeError("Telegram Bot API rejected the request")
        return payload

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        data: dict[str, object] = {"chat_id": chat_id, "text": text}
        for key, value in kwargs.items():
            if value is None:
                continue
            if key == "reply_markup" and not isinstance(value, str):
                dump_json = getattr(value, "model_dump_json", None)
                value = dump_json(exclude_none=True) if callable(dump_json) else json.dumps(value)
            data[key] = value
        await self._request("sendMessage", data=data)

    async def send_document(self, chat_id: int, path: str, *, filename: str) -> None:
        with Path(path).open("rb") as stream:
            await self._request(
                "sendDocument",
                data={"chat_id": chat_id},
                files={"document": (filename, stream, "application/octet-stream")},
            )

    async def close(self) -> None:
        await self._client.aclose()


async def _with_flood_retry(send: Callable[[], Awaitable[Any]]) -> Any:
    for attempt in range(_RETRY_AFTER_MAX_ATTEMPTS):
        try:
            return await send()
        except TelegramRetryAfter as exc:
            if attempt == _RETRY_AFTER_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(exc.retry_after + 0.1)
    return None


async def send_bot_message(bot: object, chat_id: int, text: str, **kwargs: object) -> None:
    await _with_flood_retry(lambda: bot.send_message(chat_id, text, **kwargs))  # type: ignore[attr-defined]


async def send_bot_file(bot: object, chat_id: int, *, path: str, filename: str) -> None:
    await _with_flood_retry(
        lambda: bot.send_document(chat_id, path, filename=filename)  # type: ignore[attr-defined]
    )


async def send_bot_document(bot: object, chat_id: int, *, text: str, filename: str) -> None:
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="aimash_digest_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        await send_bot_file(bot, chat_id, path=path, filename=filename)
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
