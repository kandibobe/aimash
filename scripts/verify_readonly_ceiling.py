#!/usr/bin/env python3
"""Активная проверка потолка «Read only» Хоста A (контур свободного агента).

ЗАЧЕМ. На Хосте A агент свободен (terminal + собственный код), и единственная защита чужих
денег — уровень доступа Google-пользователя, под которым выпущен refresh-токен. Эта защита
ломается МОЛЧА: повышение роли в любой иерархии снимает потолок без ошибки и без уведомления
(`deploy/hermes/RISK_REGISTER.md`, Р3). Поэтому потолок не «настраивают и забывают» — его
проверяют активно и регулярно.

ЧТО ИМЕННО ПРОВЕРЯЕТСЯ. Роль из-под read-only не прочитать (ресурс `customer_user_access`
доступен только админу аккаунта), поэтому скрипт не читает роль, а ПЫТАЕТСЯ мутировать с
`validate_only=True`: ничего не создаётся, но сервер Google обязан отказать по правам, если роль
read only. ЕДИНСТВЕННОЕ принимаемое доказательство отказа — `authorization_error.ACTION_NOT_PERMITTED`.
Любой другой исход (успех; ошибка поля; `USER_PERMISSION_DENIED`; `CUSTOMER_NOT_ENABLED`;
`ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT`) доказательством НЕ считается — это «не проверено»,
а не «зелено». Так сделано намеренно: `USER_PERMISSION_DENIED` означает «нет доступа к аккаунту»,
а не «доступ только на чтение», и именно на этой подмене проверка давала бы ложный зелёный.

ЧЕГО СКРИПТ НЕ ДЕЛАЕТ. Не проверяет личность Google-пользователя (нужен scope `userinfo`, которого
у нас нет и не должно быть). Не гарантирует будущего: это СНИМОК на момент запуска. Не видит
иерархий, в которых у пользователя нет доступа сейчас, но появится потом.

НЕГАТИВНЫЙ КОНТРОЛЬ обязателен: цепочка «отказ везде» одинаково выглядит и при исправном потолке,
и при сломанном зонде. Поэтому тот же скрипт с `--profile b` запускается на Хосте B (полноправные
креды, аккаунт Draft) и обязан НЕ получить отказ. Без пройденного негативного контроля результат
`--profile a` считается неполным.

Коды выхода:
  0 — потолок подтверждён (профиль a) / зонд различает права (профиль b);
  1 — ПОТОЛОК ПРОБИТ: сервер не отказал по правам там, где обязан был (профиль a);
  2 — НЕПОЛНО: покрытие урезано, аккаунт нечитаем, гигиена хоста нарушена, исход неоднозначен.
      Это НЕ зелёный — трактовать как «не проверено»;
  3 — ошибка запуска/конфигурации.

Запуск (Хост A, окружение MCP уже загружено):
    set -a; . /opt/aimash-read/.env; set +a
    python scripts/verify_readonly_ceiling.py --profile a

Правило 5: наружу печатаются только имена ошибок и sha8 секретов, никогда сами секреты.
Модуль намеренно НЕ импортирует `core.config` / `ads.client`: скрипт обязан работать на голой ВМ
без установленного пакета и не должен подхватывать `.env` из чужой CWD (`core/config.py:29`).
`scripts/_win_console` тоже не импортируется — вместо него три строки ниже.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from dataclasses import dataclass, field

# Windows-консоль (cp1251) роняет кириллицу в выводе; на Linux — no-op.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # не TextIOWrapper / уже настроен
        pass

# Литерал, а не импорт `core.config`: скрипт обязан работать на голой ВМ (см. докстринг). Env
# перекрывает его, чтобы .env хоста был авторитетнее кода. Равенство этого дефолта с
# `settings.google_ads_api_version` держит `tests/test_gads_version_pin.py` — иначе бамп версии в
# конфиге оставляет зонд на старой, и «потолок подтверждён» относится не к той API-версии.
API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION") or "v25"

# Единственное доказательство «сервер отказал ИМЕННО по роли».
PROOF_CODE = "authorization_error.ACTION_NOT_PERMITTED"

# Коды, которые ВЫГЛЯДЯТ как доказательство, но им не являются: каждый означает «доступа нет»
# или «аккаунт/токен не в том состоянии», а не «роль только на чтение». Держим списком явно, чтобы
# следующий читатель не «упростил» проверку до подстроки NOT_PERMITTED.
# Состав сверен с enum v24 интроспекцией SDK (`AuthorizationErrorEnum.AuthorizationError`, 18 членов):
# NOT_ADS_USER в нём ОТСУТСТВУЕТ — он живёт в `authentication_error`, и строка
# `authorization_error.NOT_ADS_USER` не могла совпасть никогда.
LOOKALIKE_CODES = (
    "authorization_error.USER_PERMISSION_DENIED",
    "authorization_error.ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT",
    "authorization_error.CUSTOMER_NOT_ENABLED",
    "authorization_error.SERVICE_ACCESS_DENIED",
    "authorization_error.ACCESS_DENIED_FOR_ACCOUNT_TYPE",
    "authorization_error.INCOMPLETE_SIGNUP",
    "authorization_error.MISSING_TOS",
    "authorization_error.INVALID_LOGIN_CUSTOMER_ID_SERVING_CUSTOMER_ID_COMBINATION",
    "authentication_error.NOT_ADS_USER",
)

OK, BREACH, INCONCLUSIVE, CONFIG_ERROR = 0, 1, 2, 3


class Incomplete(Exception):
    """Проверка не смогла покрыть периметр. Именно НЕПОЛНО (код 2), а не «потолок пробит».

    Отдельный тип нужен затем, что раньше срыв обхода летел `SystemExit(str)` и давал код 1 —
    то есть транзиентная ошибка API читалась рунбуком как «ПОТОЛОК ПРОБИТ, агента не запускать».
    Ложная тревога такого класса стоит доверия ко всей проверке.
    """


class AuthFailed(Exception):
    """Не удалось получить токен: креды не те. Это КОНФИГ (код 3), а не результат проверки.

    Тот же класс ложной тревоги, что и `Incomplete`, и найден он исполнением:
    `GoogleAdsClient.load_from_dict` обновляет OAuth-токен ПРЯМО В КОНСТРУКТОРЕ и при плохих кредах
    кидает `google.auth.exceptions.RefreshError`, а вовсе не `GoogleAdsException`. Ловушки на него
    не было — необработанное исключение дало бы код 1, который рунбук читает как «ПОТОЛОК ПРОБИТ».
    """


@dataclass
class Account:
    """Аккаунт, найденный обходом. login = контекст, под которым к нему обращаемся."""

    cid: str
    login: str | None
    name: str = ""
    is_manager: bool = False
    status: str = "UNKNOWN"
    source: str = "direct"


@dataclass
class Probe:
    """Результат одного зонда. verdict: refused | not_refused | unclear."""

    account: Account
    verdict: str
    codes: list[str] = field(default_factory=list)
    note: str = ""


def sha8(value: str) -> str:
    """Отпечаток секрета для глазной сверки. Сам секрет наружу не идёт (правило 5)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else "—"


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def build_config(login_cid: str | None) -> dict:
    """Конфиг google-ads SDK из окружения. login_customer_id — контекст авторизации."""
    cfg = {
        "developer_token": env("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": env("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": env("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": env("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus": True,
    }
    if login_cid:
        cfg["login_customer_id"] = str(login_cid)
    return cfg


class Clients:
    """Клиенты SDK по контексту login_customer_id (заголовок задаётся при сборке)."""

    def __init__(self) -> None:
        self._cache: dict[str | None, object] = {}
        self.calls = 0

    def get(self, login_cid: str | None):
        if login_cid not in self._cache:
            from google.ads.googleads.client import GoogleAdsClient

            try:
                self._cache[login_cid] = GoogleAdsClient.load_from_dict(
                    build_config(login_cid), version=API_VERSION
                )
            except Exception as e:  # noqa: BLE001 — наружу только имя типа (правило 5)
                raise AuthFailed(type(e).__name__) from None
        return self._cache[login_cid]


def error_code_names(exc) -> list[str]:
    """Имена кодов из GoogleAdsException в виде `<категория>.<КОНСТАНТА>`.

    `error_code` — proto-oneof: имя выбранного поля = категория ошибки, значение = enum.
    Реализация повторяет `core/ads_errors.error_code_name`, но без импорта пакета — скрипт обязан
    работать на хосте, где репозиторий не установлен."""
    names: list[str] = []
    failure = getattr(exc, "failure", None)
    for err in getattr(failure, "errors", None) or []:
        code = getattr(err, "error_code", None)
        if code is None:
            continue
        pb = getattr(code, "_pb", code)
        which = pb.WhichOneof("error_code") if hasattr(pb, "WhichOneof") else None
        if not which:
            continue
        value = getattr(code, which, None)
        names.append(f"{which}.{getattr(value, 'name', value)}")
    return names


def describe(clients: Clients, cid: str, login: str | None) -> tuple[bool, str, str, str]:
    """Прочитать аккаунт: (ok, имя, manager?, статус). Проверяет, что чтение вообще есть."""
    from google.ads.googleads.errors import GoogleAdsException

    query = (
        "SELECT customer.id, customer.descriptive_name, customer.manager, "
        "customer.status, customer.test_account FROM customer LIMIT 1"
    )
    try:
        clients.calls += 1
        rows = (
            clients.get(login).get_service("GoogleAdsService").search(customer_id=cid, query=query)
        )
        for row in rows:
            return (
                True,
                row.customer.descriptive_name or "",
                "manager" if row.customer.manager else "client",
                row.customer.status.name,
            )
        return False, "", "", "EMPTY"
    except AuthFailed:
        raise  # ошибка кредов — не «аккаунт не читается», а нечем проверять вообще
    except GoogleAdsException as e:
        return False, "", "", ",".join(error_code_names(e)) or type(e).__name__
    except Exception as e:  # noqa: BLE001 — наружу только имя типа (правило 5)
        return False, "", "", type(e).__name__


def discover(clients: Clients, max_accounts: int) -> tuple[list[Account], list[str]]:
    """Периметр токена: ListAccessibleCustomers + обход дочерних каждого менеджера.

    ListAccessibleCustomers возвращает ТОЛЬКО аккаунты с ПРЯМЫМ доступом — это и есть настоящий
    периметр токена; всё остальное достижимо лишь через них."""
    from google.ads.googleads.errors import GoogleAdsException

    problems: list[str] = []
    accounts: dict[str, Account] = {}

    clients.calls += 1
    try:
        res = clients.get(None).get_service("CustomerService").list_accessible_customers()
    except GoogleAdsException as e:
        # НЕ SystemExit: срыв обхода — это «периметр не покрыт» (код 2), а не «потолок пробит».
        raise Incomplete(
            f"ListAccessibleCustomers отказал: {','.join(error_code_names(e)) or 'unknown'}"
        ) from None

    direct = [rn.split("/")[-1] for rn in res.resource_names]
    for cid in direct:
        ok, name, kind, status = describe(clients, cid, None)
        if not ok:
            problems.append(f"{cid}: прямой доступ есть, но чтение не удалось ({status})")
            continue
        accounts[cid] = Account(cid, None, name, kind == "manager", status, "direct")

    # Дочерние: только под менеджером, к которому есть прямой доступ.
    query = (
        "SELECT customer_client.id, customer_client.descriptive_name, "
        "customer_client.manager, customer_client.status, customer_client.level "
        "FROM customer_client WHERE customer_client.level > 0"
    )
    for manager in [a for a in list(accounts.values()) if a.is_manager]:
        try:
            clients.calls += 1
            rows = (
                clients.get(manager.cid)
                .get_service("GoogleAdsService")
                .search(customer_id=manager.cid, query=query)
            )
            for row in rows:
                child = str(row.customer_client.id)
                if child in accounts:
                    continue
                accounts[child] = Account(
                    child,
                    manager.cid,
                    row.customer_client.descriptive_name or "",
                    bool(row.customer_client.manager),
                    row.customer_client.status.name,
                    f"child:{manager.cid}",
                )
        except GoogleAdsException as e:
            problems.append(
                f"{manager.cid}: обход дочерних не удался "
                f"({','.join(error_code_names(e)) or 'unknown'}) — иерархия НЕ покрыта"
            )

    ordered = sorted(accounts.values(), key=lambda a: a.cid)
    if len(ordered) > max_accounts:
        problems.append(
            f"найдено {len(ordered)} аккаунтов при лимите --max-accounts={max_accounts}: "
            "покрытие было бы неполным, проверка не зелёная"
        )
    return ordered, problems


def probe(clients: Clients, acc: Account) -> Probe:
    """Зонд: create бюджета с validate_only=True. Ничего не создаёт ни при каком исходе.

    Запрос собирается объектом (а не flattened-аргументами) намеренно: так `validate_only`
    гарантированно попадает в запрос, а не теряется в сигнатуре сгенерированного метода."""
    from google.ads.googleads.errors import GoogleAdsException

    client = clients.get(acc.login)
    op = client.get_type("CampaignBudgetOperation")
    budget = op.create
    budget.name = f"aimash-readonly-probe-{secrets.token_hex(4)}"
    budget.amount_micros = 10_000_000  # кратно и 10 000, и 1 000 000 (UGX/JPY)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = acc.cid
    request.operations.append(op)
    request.validate_only = True
    # НЕ assert: под `python -O` ассерты вырезаются, и единственный гард, удерживающий зонд от
    # РЕАЛЬНОГО создания бюджета в чужом аккаунте, исчез бы молча. Fail-closed явным raise.
    if request.validate_only is not True:
        raise RuntimeError("validate_only не выставлен — зонд отменён, он не смеет писать")

    try:
        clients.calls += 1
        client.get_service("CampaignBudgetService").mutate_campaign_budgets(request=request)
        return Probe(acc, "not_refused", [], "сервер принял мутацию (validate_only)")
    except GoogleAdsException as e:
        codes = error_code_names(e)
        if PROOF_CODE in codes:
            return Probe(acc, "refused", codes, "отказ по роли")
        lookalikes = [c for c in codes if c in LOOKALIKE_CODES]
        note = "похоже на отказ, но это другой смысл" if lookalikes else "неоднозначный исход"
        return Probe(acc, "unclear", codes, note)
    except AuthFailed:
        raise
    except Exception as e:  # noqa: BLE001 — правило 5
        return Probe(acc, "unclear", [], f"исключение {type(e).__name__}")


def hygiene_host_a() -> list[str]:
    """Гигиена Хоста A: чего на нём быть НЕ должно. Проверяет окружение процесса."""
    bad: list[str] = []
    if env("SECRETS_ENCRYPTION_KEY"):
        bad.append(
            "SECRETS_ENCRYPTION_KEY задан — с ним load_oauth_cache расшифрует чужие "
            "ПОЛНОПРАВНЫЕ per-account токены прямо в память процесса (ads/client.py)"
        )
    db = env("DATABASE_URL")
    if db and not db.startswith("sqlite"):
        bad.append("DATABASE_URL не sqlite — Хост A не должен ходить в БД Хоста B")
    if env("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS"):
        bad.append(
            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS не пуст — мутационный allow-list на Хосте A "
            "должен быть пустым (fail-closed, ads/client.py ensure_allowed)"
        )
    if env("ENV") in {"dev", "prod"}:
        bad.append(
            f"ENV={env('ENV')} — на Хосте A нужен test: dev открывает demo-скрипты прямой "
            "записи мимо confirm-гейта, prod требует ключ шифрования и ставит ALLOWED=all"
        )
    return bad


def _run_negative_control(clients: Clients, args) -> int:
    """Профиль b: тот же зонд на кредах С ПРАВАМИ. Обязан НЕ получить отказ."""
    if not args.account:
        print("НЕ ЗАПУЩЕНО: профиль b требует --account (аккаунт с полными правами)")
        return CONFIG_ERROR
    acc = Account(args.account, args.login, source="negative-control")
    ok, name, kind, status = describe(clients, acc.cid, acc.login)
    if not ok:
        print(f"НЕПОЛНО: аккаунт {acc.cid} не читается ({status})")
        return INCONCLUSIVE
    acc.name, acc.is_manager, acc.status = name, kind == "manager", status
    result = probe(clients, acc)
    print(f"{acc.cid} [{name}] → {result.verdict} {result.codes} ({result.note})")
    print(f"вызовов API: {clients.calls}")
    if result.verdict == "not_refused":
        print("НЕГАТИВНЫЙ КОНТРОЛЬ ПРОЙДЕН: зонд различает права (тут отказа нет).")
        return OK
    if result.verdict == "refused":
        print(
            "ПРОВАЛ КОНТРОЛЯ: отказ там, где прав достаточно — креды или зонд не те. "
            "Зелёный результат профиля a при таком зонде НИЧЕГО не значит."
        )
        return BREACH
    print("НЕПОЛНО: исход неоднозначен, различающая способность зонда не показана.")
    return INCONCLUSIVE


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка потолка Read only (снимок).")
    ap.add_argument("--profile", choices=("a", "b"), default="a")
    ap.add_argument("--account", help="профиль b: аккаунт с ЗАВЕДОМО полными правами")
    ap.add_argument("--login", help="профиль b: login_customer_id (MCC) для --account")
    ap.add_argument("--max-accounts", type=int, default=50)
    ap.add_argument(
        "--accept-inactive",
        action="store_true",
        help="не считать неполнотой пропуск аккаунтов со статусом != ENABLED",
    )
    ap.add_argument("--skip-hygiene", action="store_true")
    args = ap.parse_args()

    missing = [
        n
        for n in (
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_CLIENT_ID",
            "GOOGLE_ADS_CLIENT_SECRET",
            "GOOGLE_ADS_REFRESH_TOKEN",
        )
        if not env(n)
    ]
    if missing:
        print(f"НЕ ЗАПУЩЕНО: в окружении нет {', '.join(missing)}")
        return CONFIG_ERROR

    print("=" * 78)
    print(f"Потолок Read only — снимок. Профиль: {args.profile}. API {API_VERSION}.")
    print(
        f"refresh_token sha8={sha8(env('GOOGLE_ADS_REFRESH_TOKEN'))}  "
        f"developer_token sha8={sha8(env('GOOGLE_ADS_DEVELOPER_TOKEN'))}"
    )
    print("Сверьте sha8 глазами: на Хосте A он ОБЯЗАН отличаться от Хоста B.")
    print("=" * 78)

    clients = Clients()
    exit_code = OK
    blockers: list[str] = []

    if args.profile == "b":
        return _run_negative_control(clients, args)

    if not args.skip_hygiene:
        problems = hygiene_host_a()
        for p in problems:
            print(f"ГИГИЕНА ХОСТА: {p}")
        if problems:
            blockers.append("нарушена гигиена окружения Хоста A")

    try:
        accounts, discovery_problems = discover(clients, args.max_accounts)
    except Incomplete as e:
        print(f"НЕПОЛНО: {e}")
        print("Периметр токена не перечислен — проверять нечего. Это НЕ зелёный результат.")
        return INCONCLUSIVE
    for p in discovery_problems:
        print(f"ОБХОД: {p}")
    if discovery_problems:
        blockers.append("обход периметра неполон")

    print(f"\nНайдено аккаунтов: {len(accounts)} (прямой доступ + дочерние менеджеров)\n")
    refused = notrefused = unclear = skipped = 0
    for acc in accounts[: args.max_accounts]:
        if acc.status != "ENABLED":
            skipped += 1
            print(f"  {acc.cid:<12} {acc.status:<10} ПРОПУЩЕН (не ENABLED) — {acc.name}")
            continue
        result = probe(clients, acc)
        mark = {"refused": "ОТКАЗ", "not_refused": "ПРИНЯТО", "unclear": "НЕЯСНО"}[result.verdict]
        print(f"  {acc.cid:<12} {mark:<8} {acc.source:<16} {result.codes} {acc.name}")
        if result.verdict == "refused":
            refused += 1
        elif result.verdict == "not_refused":
            notrefused += 1
            exit_code = BREACH
        else:
            unclear += 1

    if skipped and not args.accept_inactive:
        blockers.append(f"{skipped} аккаунтов пропущено по статусу (нужен --accept-inactive)")
    if unclear:
        blockers.append(f"{unclear} аккаунтов с неоднозначным исходом")

    print("\n" + "=" * 78)
    print(f"отказ={refused}  принято={notrefused}  неясно={unclear}  пропущено={skipped}")
    print(f"вызовов API: {clients.calls} (квота Basic — 15 000 операций/сутки на весь парк)")

    if notrefused:
        print(
            "ПОТОЛОК ПРОБИТ: сервер Google принял мутацию там, где роль обязана была отказать. "
            "Агента на Хосте A НЕ ЗАПУСКАТЬ до понижения роли."
        )
        return BREACH
    if blockers:
        for b in blockers:
            print(f"НЕПОЛНО: {b}")
        print("Результат НЕ зелёный: часть периметра не проверена.")
        return INCONCLUSIVE
    print(
        "ПОТОЛОК ПОДТВЕРЖДЁН НА МОМЕНТ ЗАПУСКА. Это снимок, а не гарантия: повышение роли снимет "
        "его молча. Негативный контроль (--profile b на Хосте B) обязателен."
    )
    return exit_code


def _guarded_main() -> int:
    """Единственная точка, где ошибка кредов превращается в код 3, а не в трейсбек с кодом 1."""
    try:
        return main()
    except AuthFailed as e:
        print(f"НЕ ЗАПУЩЕНО: не удалось получить OAuth-токен ({e}).")
        print(
            "Проверьте GOOGLE_ADS_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN. Это ошибка конфигурации, "
            "а не результат проверки потолка — потолок остаётся НЕ проверенным."
        )
        return CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(_guarded_main())
