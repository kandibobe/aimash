"""Гард согласованности пина Google Ads API: одна версия во ВСЕХ точках, и она есть в SDK.

ЗАЧЕМ. Версия API живёт не в одном месте, как обещал скил `gads-version`, а в шести: дефолт
`core.config.settings.google_ads_api_version`, литерал в `scripts/verify_readonly_ceiling.py`
(он намеренно не импортирует `core.config` — работает на голой ВМ), `.env.example`/`.env.server`,
пин SDK в `pyproject.toml`, хард-пин в `constraints.txt` (его применяют Dockerfile и CI). Каждая
точка рассогласовывается МОЛЧА:

  • бампнули конфиг, забыли `constraints.txt` ⇒ прод ставит lib без нужной vNN и падает на первом
    вызове Google Ads — рядом с живым аккаунтом, а не на сборке;
  • бампнули конфиг, забыли зонд ⇒ `verify_readonly_ceiling` подтверждает потолок для ДРУГОЙ
    версии API, и «потолок подтверждён» относится не к тому, что ходит в прод;
  • оставили `google.ads.googleads.vNN.…` захардкоженным в тестах ⇒ тесты «на реальных прото»
    проверяют старую схему и остаются ЗЕЛЁНЫМИ, пока прод уже на новой.

Ни у одного из этих расхождений нет признака в логах или в поведении до боевого вызова. Поэтому
гард — здесь, на импорте/прогоне тестов, а не в чеклисте скила.

Тест НЕ знает «правильного» номера версии и не должен: он проверяет РАВЕНСТВО точек между собой и
наличие версии в установленном SDK. Какая именно версия актуальна — решает человек по release notes
(скил `gads-version`); задача гарда — чтобы решение доехало до всех точек сразу.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import tomllib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.config import settings  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = _ROOT / "scripts" / "verify_readonly_ceiling.py"
_VERSION_RE = re.compile(r"^v\d+$")

# Каталоги, где ищем хардкод прото-пути. Точки входа + слои, которые ходят в Google Ads.
_SCANNED_DIRS = (
    "ads",
    "agent",
    "app",
    "audit",
    "bot",
    "core",
    "mcp_server",
    "reports",
    "scripts",
    "scheduler",
    "tests",
)
# Хардкод пути к прото конкретной версии. Мимо `settings.google_ads_api_version` его быть не должно
# нигде: SDK выбирает версию на запрос, и литерал в коде переживает бамп пина незамеченным.
_HARDCODED_PROTO_RE = re.compile(r"google\.ads\.googleads\.v\d+")


def _probe_default_version() -> str:
    """Дефолт `API_VERSION` из зонда, добытый AST (а не импортом: модуль — CLI-скрипт с argparse).

    Форма в зонде: `API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION") or "vNN"`. Берём правый
    операнд `or` — это и есть встроенный дефолт, который применится на хосте без переменной."""
    tree = ast.parse(_PROBE.read_text(encoding="utf-8"), filename=str(_PROBE))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "API_VERSION" for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            value = value.values[-1]
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{_PROBE.name}: API_VERSION перестал быть строковым литералом/`env or литерал` — "
            "гард не может его прочитать, значит согласованность пина больше не проверяется"
        )
        return value.value
    raise AssertionError(f"{_PROBE.name}: не найдено присваивание API_VERSION на уровне модуля")


def _env_pin(name: str) -> str:
    """Значение GOOGLE_ADS_API_VERSION из .env-шаблона (`.env.example` / `.env.server`)."""
    text = (_ROOT / name).read_text(encoding="utf-8")
    match = re.search(r"^GOOGLE_ADS_API_VERSION=(\S+)", text, re.MULTILINE)
    assert match, f"{name}: строка GOOGLE_ADS_API_VERSION= пропала — шаблон перестал пинить версию"
    return match.group(1)


# `.env.example` — в репо (обязан быть). `.env.server` — под `.gitignore:3` (`.env.*`): он живёт
# ТОЛЬКО у оператора, в чистом клоне и на CI его нет по построению. Читать его безусловно = красный
# CI на файле, которого там не может быть; молча пропускать оба = гард, который никогда не срабатывает.
# Поэтому асимметрия явная: отсутствует — skip, присутствует — проверяем наравне с остальными точками.
_ENV_TEMPLATES = ((".env.example", True), (".env.server", False))


def test_configured_version_is_well_formed():
    """`vNN` и ничего больше: минор (`v25.2`) в этом поле сломает импорт прото-пакета."""
    version = settings.google_ads_api_version
    assert _VERSION_RE.match(version), (
        f"google_ads_api_version='{version}' — ожидается мажор вида 'v25' (SDK бандлит пакеты "
        "именно по мажору; минорные версии API выбираются сервером, а не путём импорта)"
    )


def test_installed_sdk_bundles_configured_version():
    """Установленный `google-ads` обязан содержать настроенную vNN.

    Иначе первое обращение к Google Ads падает в проде: `GoogleAdsClient.load_from_dict(...,
    version=...)` резолвит версию в пакет `google.ads.googleads.vNN`. Проверка — импортом, а не
    сравнением с таблицей версий в доке: таблица устаревает, установленный пакет — нет."""
    import importlib

    version = settings.google_ads_api_version
    try:
        importlib.import_module(f"google.ads.googleads.{version}")
    except ImportError as e:  # noqa: PERF203 — сообщение важнее компактности
        import importlib.metadata as md
        import pkgutil

        import google.ads.googleads as pkg

        bundled = sorted(
            m.name for m in pkgutil.iter_modules(pkg.__path__) if _VERSION_RE.match(m.name)
        )
        raise AssertionError(
            f"установленный google-ads {md.version('google-ads')} НЕ бандлит {version} "
            f"(есть: {bundled}). Пин API и пин SDK разошлись — прод упадёт на первом вызове "
            f"Google Ads. Поднимите google-ads в pyproject.toml и constraints.txt ({e})"
        ) from e


def test_probe_pin_matches_config():
    """Зонд потолка read-only проверяет ТУ ЖЕ версию, что ходит в прод.

    Зонд не импортирует `core.config` осознанно (голая ВМ), поэтому его дефолт — второй литерал в
    репо. Разойдись он с конфигом — `verify_readonly_ceiling --profile a` продолжит печатать
    «потолок подтверждён», подтверждая права для версии, которой в проде уже нет."""
    assert _probe_default_version() == settings.google_ads_api_version, (
        f"{_PROBE.name}: API_VERSION='{_probe_default_version()}', а "
        f"settings.google_ads_api_version='{settings.google_ads_api_version}'"
    )


@pytest.mark.parametrize(("name", "required"), _ENV_TEMPLATES)
def test_env_templates_pin_matches_config(name: str, required: bool):
    """`.env.example` и `.env.server` пинят ту же версию, что дефолт в конфиге.

    Шаблон — то, что оператор копирует на хост; разойдись он с кодом, прод поедет на версии из
    шаблона, а тесты и зонд — на версии из конфига, и никто этого не увидит."""
    if not (_ROOT / name).exists():
        assert not required, f"{name}: шаблон пропал из репозитория — пинить версию стало нечему"
        pytest.skip(f"{name} под .gitignore и на этой машине отсутствует (чистый клон / CI)")
    assert _env_pin(name) == settings.google_ads_api_version, (
        f"{name}: GOOGLE_ADS_API_VERSION={_env_pin(name)}, а в core/config.py — "
        f"{settings.google_ads_api_version}"
    )


def test_constraints_pin_satisfies_pyproject_specifier():
    """Хард-пин `google-ads` в `constraints.txt` удовлетворяет диапазону из `pyproject.toml`.

    Именно `constraints.txt` применяют `Dockerfile` и CI (`pip install -c constraints.txt`), а
    диапазон в `pyproject.toml` — то, что читает человек. Разойдись они, образ соберётся на версии
    SDK, которую никто не декларировал, — включая lib без нужной vNN."""
    from packaging.requirements import Requirement
    from packaging.version import Version

    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = next(
        Requirement(dep).specifier
        for dep in pyproject["project"]["dependencies"]
        if Requirement(dep).name == "google-ads"
    )
    constraints = (_ROOT / "constraints.txt").read_text(encoding="utf-8")
    match = re.search(r"^google-ads==(\S+)", constraints, re.MULTILINE)
    assert match, "constraints.txt: пин google-ads== пропал — CI/Docker поедут на любой версии SDK"
    pinned = Version(match.group(1))
    assert pinned in spec, (
        f"constraints.txt пинит google-ads=={pinned}, что НЕ удовлетворяет '{spec}' из "
        "pyproject.toml — образ соберётся на недекларированной версии SDK"
    )


def test_no_hardcoded_proto_version_path():
    """Ни одного `google.ads.googleads.vNN.…` в коде и тестах — только через конфиг.

    Это и есть закрытие КЛАССА бага, а не его экземпляра: пока литерал версии допустим в исходнике,
    следующий бамп снова оставит часть репо на прошлой схеме, и суд-тесты «на реальных прото»
    подтвердят версию, которой в проде нет. В тестах путь собирается `gads_proto()` из
    `settings.google_ads_api_version` (`tests/test_write_layer.py`), в проде — параметром `version=`
    у `GoogleAdsClient.load_from_dict` (`ads/client.py`)."""
    offenders: list[str] = []
    for directory in _SCANNED_DIRS:
        for path in (_ROOT / directory).rglob("*.py"):
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue  # сам гард обязан содержать этот паттерн — он его и ищет
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _HARDCODED_PROTO_RE.search(line):
                    offenders.append(
                        f"{path.relative_to(_ROOT).as_posix()}:{lineno}: {line.strip()}"
                    )
    assert not offenders, (
        "захардкожен путь к прото конкретной API-версии — при бампе пина эти места молча останутся "
        "на старой схеме:\n  " + "\n  ".join(offenders)
    )
