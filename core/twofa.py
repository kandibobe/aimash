"""§12 2FA (опц. в ТЗ): PIN-подтверждение ОПАСНЫХ операций Google Ads ПЕРЕД исполнением.

Opt-in, дефолт OFF, fail-closed. Замысел (golden rule #1/#3/#10):
- Гейт стоит в bot.main._do_confirm ДО перевода черновика в confirmed/исполнения. Пока код не
  введён — черновик остаётся `pending` (не сожжён): неверный код/отмена → повторить ✅ можно.
- PIN сверяет КОД (hmac.compare_digest — constant-time), НЕ модель и НЕ промпт. Сырьё PIN/кода
  НЕ логируется (SecretStr в конфиге + отсутствие print/log здесь).
- Fail-closed: 2FA включён, но PIN не задан ⇒ `is_ready()` False ⇒ гейт БЛОКИРУЕТ опасные
  операции (не пропускает без проверки). Выключен ⇒ поведение как раньше (ничего не гейтим).

Это НЕ шифрование и не защита от компрометации самого Telegram-аккаунта — это второй фактор
«кто-то нажал ✅» против случайного/поспешного подтверждения денежной/необратимой операции.
"""

from __future__ import annotations

import hmac

from core.config import settings


def enabled() -> bool:
    """Включён ли 2FA-гейт (env TWO_FACTOR_ENABLED). Дефолт False — поведение не меняется."""
    return bool(settings.two_factor_enabled)


def _pin() -> str:
    """Сырой PIN из секрета (без логирования). Пустой → '' (⇒ fail-closed в is_ready/verify)."""
    try:
        return (settings.two_factor_pin.get_secret_value() or "").strip()
    except Exception:  # noqa: BLE001 — отсутствие/битый секрет ⇒ считаем PIN пустым (fail-closed)
        return ""


def is_ready() -> bool:
    """2FA включён И PIN задан. Включён без PIN ⇒ False ⇒ гейт fail-closed блокирует опасные ops
    (не открывает их без проверки)."""
    return enabled() and bool(_pin())


def required_for(operation: str) -> bool:
    """Нужен ли 2FA-код для операции. Выключен ⇒ False (никогда). Включён ⇒ True для операций из
    two_factor_ops (в т.ч. когда PIN не задан — тогда _twofa_begin заблокирует, fail-closed)."""
    if not enabled():
        return False
    return str(operation) in settings.two_factor_ops


def verify(code: str) -> bool:
    """Constant-time сверка введённого кода с PIN. Пустой PIN или пустой код ⇒ False (fail-closed).
    hmac.compare_digest не «утекает» длину/позицию несовпадения по времени."""
    pin = _pin()
    code = (code or "").strip()
    if not pin or not code:
        return False
    return hmac.compare_digest(code, pin)
