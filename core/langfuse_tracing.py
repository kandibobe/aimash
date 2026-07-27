"""Langfuse observability — трейсинг LLM-вызовов через нативную OpenAI SDK интеграцию.

Принципы (Langfuse best practices):
  • framework integration preferred — langfuse.openai проксирует AsyncOpenAI,
    автоматически захватывая model/tokens/стоимость/спаны без ручного кода;
  • импорт ПОСЛЕ загрузки env-переменных (common mistake: langfuse инициализируется
    до load_dotenv → пустые креды → traces молча теряются);
  • flush() перед graceful shutdown (без него последние traces не отправлены);
  • session_id = Telegram chat_id (группирует сообщения в сессии);
  • user_id = Telegram user_id (фильтрация и cost attribution);
  • feature тег = команда/роль (per-feature аналитика);
  • маскировка секретов: все SecretStr НЕ попадают в trace input/output.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("aimash.langfuse")

_langfuse_client: Any = None
_langfuse_enabled: bool = False


def _get_creds() -> tuple[str, str, str]:
    """Извлечь ключи Langfuse из переменных окружения (не из config —
    чтобы не зависеть от Settings для Langfuse-специфичных параметров).
    Переменные: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST."""
    import os

    pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    sk = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "") or os.getenv("LANGFUSE_BASE_URL", "")
    if not host:
        host = "https://cloud.langfuse.com"  # EU cloud default
    return pk, sk, host


def is_enabled() -> bool:
    """True, если Langfuse активен после успешного init."""
    return _langfuse_enabled


def get_client() -> Any | None:
    """Langfuse клиент или None, если не инициализирован/выключен."""
    return _langfuse_client


def init_langfuse() -> None:
    """Инициализировать Langfuse клиент. Должен вызываться ПОСЛЕ загрузки .env
    (load_dotenv / core.config.Settings) и ДО первого импорта langfuse.openai.

    Fail-soft: как Sentry, Langfuse — опциональная телеметрия, НЕ валит старт.
    Без ключей → молча no-op (observability выключена, бот работает как раньше).
    """
    global _langfuse_client, _langfuse_enabled

    pk, sk, host = _get_creds()
    if not pk or not sk:
        log.debug("Langfuse: ключи не заданы — трейсинг выключен (no-op)")
        return

    try:
        import langfuse

        _langfuse_client = langfuse.Langfuse(
            public_key=pk,
            secret_key=sk,
            host=host,
        )
        _langfuse_enabled = True
        log.info("Langfuse трейсинг включён (host=%s)", host)
    except ImportError:
        log.warning(
            "Langfuse: ключи заданы, но пакет langfuse не установлен — трейсинг выключен"
        )
    except Exception as e:
        log.warning(
            "Langfuse init не удался (%s) — продолжаю без трейсинга", type(e).__name__
        )


def flush_langfuse() -> None:
    """Отправить все буферизированные traces. Вызывать при graceful shutdown."""
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
            log.debug("Langfuse flush — traces отправлены")
        except Exception as e:
            log.warning("Langfuse flush провалился (%s)", type(e).__name__)


def create_trace(
    name: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    input_data: Any = None,
) -> Any | None:
    """Создать trace верхнего уровня. Возвращает trace-объект Langfuse или None.

    Args:
        name: имя трассы (напр. 'parsing-command', 'generate-rsa')
        session_id: Telegram chat_id (группировка диалогов)
        user_id: Telegram user_id (атрибуция)
        tags: ['parsing', 'mutation'] — фильтрация в дашборде
        metadata: произвольные метаданные (account_id, campaign_id)
        input_data: входные данные трассы (сообщение пользователя)
    """
    if not _langfuse_enabled or _langfuse_client is None:
        return None
    try:
        trace = _langfuse_client.trace(
            name=name,
            session_id=session_id,
            user_id=user_id,
            tags=tags or [],
            metadata=metadata,
            input=input_data,
        )
        return trace
    except Exception as e:
        log.debug("Langfuse trace creation failed (%s)", type(e).__name__)
        return None