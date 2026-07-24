"""Гард: `.env.defaults` не дублирует код-дефолты (иначе он их МОЛЧА замораживает).

Инцидент 2026-07-14: `.env.defaults` отслеживается git, деплоится на VPS и стоит ПЕРВЫМ в
`env_file: [.env.defaults, .env]` (docker-compose.yml) — то есть его строки реально применяются
в проде и перебивают поля `Settings`. Файл держал `CRAWL_MAX_PAGES=70` / `CRAWL_TIME_BUDGET_S=90`
— значения, равные код-дефолтам НА МОМЕНТ его написания. Потом код-дефолты выросли до 1000/240,
а прод остался на 70/90: обход сайта §20 молча резался втрое. `test_env_example_drift` этого не
видит — он сверяет только `.env.example` ↔ `Settings`.

Два правила, которые здесь закрепляются:

1. В `.env.defaults` попадает ТОЛЬКО значение, ОТЛИЧНОЕ от код-дефолта (осознанное прод-отклонение).
   Совпало с дефолтом — строку удалить: код и так даст это значение, а строка завтра заморозит новое.
2. Каждый оверрайд ОБЪЯВЛЯЕТ, против какого код-дефолта он написан — строкой `# code-default: <X>`
   прямо над ним. Уехал дефолт в коде — тест краснеет и требует пересмотреть оверрайд.
   **Это и есть защита от инцидента:** `CRAWL_MAX_PAGES=70` был законным оверрайдом (код давал 50),
   а стал тихим даунгрейдом, когда код уехал на 1000. Правило 1 такое не ловит — ловит правило 2.

Плюс два инварианта файла: никаких секретов и никаких inline-комментариев (парсер env_file у
docker-compose утаскивает хвост `# …` в значение пустой переменной — так в конфиг уже протекал
нормализованно-пустой customer_id).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pydantic import SecretStr, TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / ".env.defaults"

# Ключи, читаемые НЕ через Settings (os.getenv напрямую), + их код-дефолт для сверки.
_NON_SETTINGS_DEFAULTS: dict[str, str] = {
    "LOG_LEVEL": "INFO",  # core/logging.py::_resolve_level — пусто ⇒ INFO
    "LOG_FORMAT": "text",  # core/logging.py::setup_logging — пусто ⇒ text
}

# Секреты в отслеживаемом git файле недопустимы (правило #5). SecretStr-поля ловятся по типу,
# пароли БД приходят в compose из untracked .env — здесь их быть не должно.
_BANNED_KEYS = {"POSTGRES_PASSWORD", "POSTGRES_RO_PASSWORD"}


def _lines() -> list[tuple[int, str, str]]:
    """[(номер строки, KEY, raw value)] — только строки вида KEY=value."""
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(DEFAULTS.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out.append((i, m.group(1), m.group(2).strip()))
    return out


def _declared_code_defaults() -> dict[str, str]:
    """{KEY: значение из `# code-default: X` над строкой KEY=…}."""
    declared: dict[str, str] = {}
    pending: str | None = None
    for line in DEFAULTS.read_text(encoding="utf-8").splitlines():
        m_decl = re.match(r"^#\s*code-default:\s*(.*)$", line.strip())
        if m_decl:
            pending = m_decl.group(1).strip()
            continue
        m_kv = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m_kv:
            if pending is not None:
                declared[m_kv.group(1)] = pending
            pending = None
    return declared


def _fmt_default(value: object) -> str:
    """Код-дефолт → строка в том виде, в каком он пишется в env-файле."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _strip_inline_comment(value: str) -> str:
    """Срезать хвост `# …` — как это (частично) делает парсер env_file; сам факт хвоста ловит
    отдельный тест, здесь он не должен ломать сверку значений."""
    return re.sub(r"\s+#.*$", "", value).strip()


def _code_default(key: str) -> object:
    field = Settings.model_fields[key.lower()]
    return field.default


def _coerce(key: str, raw: str) -> object:
    """Привести строку из env-файла к типу поля Settings (как это сделал бы pydantic-settings)."""
    ann = Settings.model_fields[key.lower()].annotation
    return TypeAdapter(ann).validate_python(raw)


def test_env_defaults_has_no_unknown_keys():
    """Опечатка в ключе = строка, которая молча ничего не настраивает."""
    fields = {f.upper() for f in Settings.model_fields}
    unknown = sorted(
        {k for _, k, _ in _lines()} - fields - set(_NON_SETTINGS_DEFAULTS) - _BANNED_KEYS
    )
    assert not unknown, (
        f".env.defaults содержит неизвестные ключи (опечатка/удалённое поле?): {unknown}"
    )


def test_env_defaults_has_no_secrets():
    """Файл в git — секретам здесь не место (золотое правило #5)."""
    leaked = []
    for _, key, _ in _lines():
        if key in _BANNED_KEYS:
            leaked.append(key)
            continue
        field = Settings.model_fields.get(key.lower())
        if field is not None and field.annotation is SecretStr:
            leaked.append(key)
    assert not leaked, (
        f".env.defaults ОТСЛЕЖИВАЕТСЯ git — секреты в нём недопустимы: {sorted(leaked)}. "
        "Место секретов — untracked серверный .env."
    )


def test_env_defaults_has_no_inline_comments():
    """`KEY=  # коммент` → парсер env_file у docker-compose кладёт хвост в ЗНАЧЕНИЕ."""
    bad = [(i, k) for i, k, v in _lines() if "#" in v]
    assert not bad, (
        f".env.defaults: inline-комментарий в значении (строки {[i for i, _ in bad]}) — "
        "docker-compose утащит хвост '# …' в значение. Комментарий — на отдельной строке ВЫШЕ."
    )


def test_env_defaults_only_overrides_code_defaults():
    """Ядро гарда: каждая строка должна ОТЛИЧАТЬСЯ от код-дефолта, иначе она его замораживает."""
    redundant: list[str] = []
    for _, key, written in _lines():
        raw = _strip_inline_comment(written)
        if key in _NON_SETTINGS_DEFAULTS:
            if raw == _NON_SETTINGS_DEFAULTS[key]:
                redundant.append(f"{key}={raw}")
            continue
        if key.lower() not in Settings.model_fields:
            continue  # покрыто test_env_defaults_has_no_unknown_keys
        if _coerce(key, raw) == _code_default(key):
            redundant.append(f"{key}={raw}")
    assert not redundant, (
        "В .env.defaults строки, ДУБЛИРУЮЩИЕ код-дефолт: "
        f"{redundant}. Удали их: код и так даёт это значение, а строка МОЛЧА заморозит будущее "
        "(так прод сидел на CRAWL_MAX_PAGES=70, когда в коде уже было 1000). В .env.defaults "
        "оставляем только осознанные прод-отклонения."
    )


def test_every_override_declares_the_code_default_it_overrides():
    """Ядро гарда: оверрайд объявляет код-дефолт, против которого написан. Уехал дефолт — красный
    тест заставляет ПЕРЕСМОТРЕТЬ оверрайд (а не молча зафиксировать вчерашнее значение на проде).

    Ровно этого не хватало: CRAWL_MAX_PAGES=70 против код-дефолта 50 был осознанным, а когда код
    уехал на 1000 — прод остался на 70 и никто не узнал."""
    declared = _declared_code_defaults()
    problems: list[str] = []
    for _, key, _written in _lines():
        if key in _NON_SETTINGS_DEFAULTS:
            actual = _NON_SETTINGS_DEFAULTS[key]
        elif key.lower() in Settings.model_fields:
            actual = _fmt_default(_code_default(key))
        else:
            continue  # покрыто test_env_defaults_has_no_unknown_keys
        if key not in declared:
            problems.append(f"{key}: нет строки '# code-default: {actual}' над оверрайдом")
        elif declared[key] != actual:
            problems.append(
                f"{key}: код-дефолт уехал ({declared[key]!r} → {actual!r}) — пересмотри оверрайд "
                f"(нужен ли он ещё?) и обнови '# code-default:'"
            )
    assert not problems, "\n".join(problems)
