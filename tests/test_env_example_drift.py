"""B5+B6: ENV валидируется (fail-fast на опечатку) + .env.example не дрейфит от Settings.

B5: ENV ∉ {dev,test,prod} раньше молча трактовалось как НЕ-prod → все prod-гейты безопасности
тихо отключались. Теперь неизвестное значение падает на старте.
B6: каждое поле Settings присутствует в .env.example (шаблон не отстаёт при добавлении настройки).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# env-ключи в .env.example, читаемые НЕ через Settings (os.getenv напрямую) — легитимно не-поля.
# POSTGRES_*_PASSWORD читает docker-compose.yml (подставляет в postgres/DATABASE_URL/бэкап) —
# в Python они не приходят, приложение видит уже собранный DATABASE_URL.
# DISABLE_ALL_MUTATIONS (BZ-1) полем Settings быть НЕ ДОЛЖЕН: settings — снимок, взятый один раз на
# старте, а аварийный рубильник обязан действовать без рестарта, поэтому `core.killswitch` читает
# его живьём из os.environ. Стань он полем — рубильник молча получил бы задержку в один рестарт.
_NON_SETTINGS_KEYS = {
    "LOG_LEVEL",
    "LOG_FORMAT",
    "POSTGRES_PASSWORD",
    "POSTGRES_RO_PASSWORD",
    "DISABLE_ALL_MUTATIONS",
}


# ── B5: валидация имени ENV ────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["production", "PROD ", "staging", "develop", "prd", ""])
def test_invalid_env_name_fails_fast(bad):
    with pytest.raises(ValidationError):
        Settings(env=bad, _env_file=None)


@pytest.mark.parametrize("good,norm", [("dev", "dev"), ("PROD", "prod"), (" Test ", "test")])
def test_valid_env_name_normalized(good, norm):
    # prod требует доп. поля — проверяем нормализацию на dev/test; для prod задаём минимум
    if norm == "prod":
        from cryptography.fernet import Fernet

        s = Settings(
            env=good,
            secrets_encryption_key=Fernet.generate_key().decode(),
            telegram_whitelist_chat_ids="1",
            google_ads_developer_token="t",
            google_ads_allowed_customer_ids="7753643025",
            # D3: дефолтный DATABASE_URL несёт утёкший в git пароль — в prod он отвергается
            database_url="postgresql+asyncpg://aimash:S3cret@db:5432/aimash",
            _env_file=None,
        )
    else:
        s = Settings(env=good, _env_file=None)
    assert s.env == norm


# ── B6: .env.example покрывает все поля Settings ───────────────────────────────────
def _example_keys() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


def test_env_example_covers_all_settings_fields():
    fields = {f.upper() for f in Settings.model_fields.keys()}
    # Internal role defaults remain supported in Settings for library callers, but are deliberately
    # absent from the Hermes v3 installation template: Hermes owns its ReAct model configuration.
    internal_llm_roles = {"LLM_PARSING", "LLM_COPY", "LLM_KEYWORDS", "LLM_ANALYST"}
    missing = sorted(fields - _example_keys() - internal_llm_roles)
    assert not missing, (
        f".env.example отстал от Settings — нет ключей: {missing}. Допиши их в .env.example "
        "(шаблон — источник для чистой установки)."
    )


def test_env_example_has_no_unknown_keys():
    """Обратный дрейф: каждый ключ .env.example — либо поле Settings, либо явный не-Settings (логи)."""
    fields = {f.upper() for f in Settings.model_fields.keys()}
    unknown = sorted(_example_keys() - fields - _NON_SETTINGS_KEYS)
    assert not unknown, (
        f".env.example содержит неизвестные ключи (опечатка/удалённое поле?): {unknown}"
    )
