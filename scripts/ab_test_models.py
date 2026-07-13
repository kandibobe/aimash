"""A/B-тест моделей (Фаза −1) — выбираем модель ПО ДАННЫМ, не по бренду.

Прогоняет реальные русские команды через несколько моделей (DeepSeek / GPT / Claude)
через OpenRouter и проверяет:
  1) function calling: выбрана ли правильная функция и аргументы (ловушки «на 20%» vs «до 20%»,
     суммы/валюта, типы соответствия broad/phrase/exact, неоднозначное → должна УТОЧНИТЬ);
  2) генерация RSA-текстов: укладывается ли в лимиты (кириллица = 1 символ), без CAPS;
  3) СКОРОСТЬ (TTFT) — время-до-первого-токена парс-запроса: то, что реально ждёт пользователь.
     Парсинг — денежный путь и самый чувствительный к задержке, поэтому скорость = отдельная ось.

Запуск:  python scripts/ab_test_models.py
Нужен только OPENROUTER_API_KEY в .env. Печатает таблицу: автоматический скоринг (функции +
длина + TTFT) + сэмплы для ручной оценки качества русского (его автоматом не оценить).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from agent.loop import SYSTEM  # noqa: E402 — боевой системный промпт, не копия
from agent.tools.schemas import SCHEMAS, TOOLS  # noqa: E402 — боевые схемы тулов, не копии
from core.config import settings  # noqa: E402

# Кандидаты с tool use (Hermes выбыл — нет function calling на OpenRouter). Сравниваем точность
# парсинга, длину текстов и СКОРОСТЬ (TTFT). deepseek дёшев; gpt-4o-mini — быстрый/надёжный на
# tool use; gpt-4o/claude — посильнее. Свой набор — правь список или env MODEL_CHOICES.
CANDIDATES = [
    "deepseek/deepseek-chat",
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4.6",
]

# SYSTEM и TOOLS — БОЕВЫЕ (импорт выше), а не копии. Раньше скрипт нёс свои: их enum режимов
# отстал от `agent/tools/schemas.py` (не знал decrease_*), и A/B мерил контракт, которого нет в
# проде. Смысл теста — «пройдёт ли модель ДЕНЕЖНЫЙ путь», значит и промпт, и схемы должны быть те же.

# (команда, ожидаемая функция, проверка аргументов, комментарий)
# Аргументы приходят УЖЕ прогнанные через боевую Pydantic-схему (см. validate_args): проверяем то,
# что реально увидел бы confirm-гейт, а не сырой JSON модели. Кампания указана везде, где схема её
# требует, — иначе правильный ответ модели был бы ask_clarification, и сценарий мерил бы не то.
SCENARIOS = [
    (
        "повысь бюджет кампании Лето на 20%",
        "update_budget",
        lambda a: a.get("mode") == "increase_by_percent" and a.get("value") == 20,
        "на 20% = increase_by_percent, не set_to",
    ),
    (
        "снизь бюджет кампании Лето на 20%",
        "update_budget",
        lambda a: a.get("mode") == "decrease_by_percent" and a.get("value") == 20,
        "§5: «снизь на 20%» = decrease_by_percent, value>0 (не increase, не -20)",
    ),
    (
        "урежь бюджет кампании Зима на 50 в день",
        "update_budget",
        lambda a: a.get("mode") == "decrease_by_amount" and a.get("value") == 50,
        "§5: «урежь на 50» = decrease_by_amount",
    ),
    (
        "смени бюджет кампании X на $50 в день",
        "update_budget",
        lambda a: a.get("mode") in ("set_to", "increase_by_amount")
        and a.get("value") == 50
        and a.get("currency") == "USD",
        "$50 в день, валюта USD",
    ),
    (
        "подними ставку до 10 грн в кампании Лето",
        "update_bid",
        lambda a: a.get("mode") == "set_to" and a.get("value") == 10 and a.get("currency") == "UAH",
        "'до 10' = set_to, грн = UAH",
    ),
    (
        "сбрось ставку на 15% в кампании Осень",
        "update_bid",
        lambda a: a.get("mode") == "decrease_by_percent" and a.get("value") == 15,
        "§5: «сбрось на 15%» = decrease_by_percent",
    ),
    (
        "в кампанию Лето добавь ключи: купить телефон, цена телефона — фразовое соответствие",
        "add_keywords",
        lambda a: a.get("match_type") == "phrase" and len(a.get("keywords", [])) == 2,
        "фразовое = phrase, 2 ключа",
    ),
    (
        "в кампанию Лето добавь точное соответствие: ремонт обуви киев",
        "add_keywords",
        lambda a: a.get("match_type") == "exact",
        "точное = exact",
    ),
    (
        "поставь на паузу кампанию Зима",
        "pause_campaign",
        lambda a: "Зима" in (a.get("campaign") or ""),
        "пауза кампании",
    ),
    (
        "добавь минус-слово бесплатно в кампанию Зима",
        "add_negative_keywords",
        lambda a: any("бесплатно" in k for k in a.get("keywords", [])),
        "минус-слово",
    ),
    (
        "в кампании Лето измени ГЕО на Киев + радиус 10 км",
        "set_geo_proximity",
        lambda a: a.get("radius_km") == 10 and "иев" in (a.get("city_name") or "").lower(),
        "радиус 10 км, Киев",
    ),
    (
        "покажи статистику по аккаунту 123 за последние 30 дней",
        "get_stats",
        lambda a: a.get("period_days") == 30,
        "read-only, 30 дней",
    ),
    (
        "увеличь бюджет",
        "ask_clarification",
        lambda a: True,
        "НЕОДНОЗНАЧНО → должен уточнить, не угадывать",
    ),
    (
        "повысь ставку на 15% в кампании Осень",
        "update_bid",
        lambda a: a.get("mode") == "increase_by_percent" and a.get("value") == 15,
        "на 15% = increase_by_percent",
    ),
]

COPY_PROMPT = (
    "Сгенерируй 5 заголовков (каждый ≤30 символов) и 2 описания (каждое ≤90 символов) "
    "для рекламы доставки цветов в Киеве на русском. Верни строго JSON: "
    '{"headlines": [...], "descriptions": [...]}. Без CAPS LOCK и спам-символов.'
)


def rsa_len(text: str) -> int:
    def width(ch: str) -> int:
        o = ord(ch)
        cjk = 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7A3
        return 2 if cjk else 1

    return sum(width(c) for c in text)


def client() -> AsyncOpenAI:
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        print("❌ OPENROUTER_API_KEY не задан в .env — добавь ключ и перезапусти.")
        sys.exit(1)
    return AsyncOpenAI(api_key=key, base_url=settings.openrouter_base_url)


def validate_args(fn: str, args: dict) -> tuple[dict, str | None]:
    """Прогоняем аргументы через БОЕВУЮ Pydantic-схему — в проде их видит именно она (agent/loop.py).
    Модель, выдавшая value=-20 или currency='percent' при set_to, до кнопок ✅ не доходит, значит и
    в A/B это провал сценария, а не «почти правильно». Возвращаем (нормализованные args, ошибка)."""
    schema = SCHEMAS.get(fn)
    if schema is None:  # ask_clarification и прочие без Pydantic-схемы
        return args, None
    try:
        return schema(**args).model_dump(), None
    except ValidationError as e:
        err = e.errors()[0]
        return args, f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"


async def run_scenario(cli: AsyncOpenAI, model: str, command: str) -> tuple[str, dict, str | None]:
    resp = await cli.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": command}],
        tools=TOOLS,
        tool_choice="auto",
    )
    msg = resp.choices[0].message
    if not msg.tool_calls:
        return "(no tool call)", {}, None
    call = msg.tool_calls[0]
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    args, err = validate_args(call.function.name, args)
    return call.function.name, args, err


async def run_copy(cli: AsyncOpenAI, model: str) -> dict:
    resp = await cli.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": COPY_PROMPT}],
    )
    text = resp.choices[0].message.content or ""
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"headlines": [], "descriptions": []}


async def measure_ttft(cli: AsyncOpenAI, model: str, command: str, n: int = 3) -> float | None:
    """Среднее время-до-первого-токена (TTFT) парс-запроса по n прогонам, в мс — то, что реально
    ждёт пользователь в «печатает…». Стримим ответ и засекаем задержку до первого чанка. None, если
    все прогоны упали. TTFT — отдельная ось от точности/цены: «скорость», а не «правильность»."""
    samples: list[float] = []
    for _ in range(n):
        t0 = time.monotonic()
        stream = None
        try:
            stream = await cli.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": command},
                ],
                tools=TOOLS,
                tool_choice="auto",
                stream=True,
            )
            async for _chunk in stream:  # первый чанк = первый токен → фиксируем TTFT и выходим
                samples.append((time.monotonic() - t0) * 1000)
                break
        except Exception:  # noqa: BLE001 — один упавший прогон не валит весь замер
            continue
        finally:
            if stream is not None:
                try:
                    await stream.close()  # не копим открытые соединения после break
                except Exception:  # noqa: BLE001
                    pass
    return sum(samples) / len(samples) if samples else None


async def test_model(cli: AsyncOpenAI, model: str) -> dict:
    print(f"\n=== {model} ===")
    fc_pass = 0
    for command, expected_fn, check, note in SCENARIOS:
        try:
            fn, args, err = await run_scenario(cli, model, command)
        except Exception as e:  # модель/сеть
            print(f"  ✗ ERROR  «{command}» → {type(e).__name__}: {e}")
            continue
        ok = fn == expected_fn and err is None and check(args)
        fc_pass += int(ok)
        mark = "✓" if ok else "✗"
        why = f"  ⛔ схема отвергла: {err}" if err else ""
        print(f"  {mark} «{command}» → {fn} {args}{why}  [{note}]")

    # Скорость: TTFT парс-запроса (то, что ждёт пользователь). Отдельная ось от точности/цены.
    ttft = await measure_ttft(cli, model, SCENARIOS[0][0])
    print(
        f"  TTFT парсинга (среднее 3): {f'{ttft:.0f} мс' if ttft is not None else '— (стрим недоступен)'}"
    )

    # Тексты: автоматически — только длина; качество русского оцени глазами по сэмплам.
    copy = await run_copy(cli, model)
    heads = copy.get("headlines", [])
    descs = copy.get("descriptions", [])
    len_ok = sum(rsa_len(h) <= 30 for h in heads) + sum(rsa_len(d) <= 90 for d in descs)
    len_tot = len(heads) + len(descs)
    print(f"  RSA длина: {len_ok}/{len_tot} в лимите. Сэмплы (оцени русский вручную):")
    for h in heads[:3]:
        print(f"      [{rsa_len(h):>2}] {h}")

    return {
        "model": model,
        "fc_pass": fc_pass,
        "fc_total": len(SCENARIOS),
        "copy_len_ok": len_ok,
        "copy_total": len_tot,
        "ttft_ms": ttft,
    }


async def main() -> None:
    cli = client()
    results = []
    for model in CANDIDATES:
        try:
            results.append(await test_model(cli, model))
        except Exception as e:
            print(f"\n=== {model} === недоступна: {e}")

    print("\n\n================= ИТОГ =================")
    print(f"{'модель':<32} {'функции':>10} {'тексты':>10} {'TTFT мс':>10}")
    for r in sorted(results, key=lambda x: -x["fc_pass"]):
        ttft = f"{r['ttft_ms']:.0f}" if r.get("ttft_ms") is not None else "—"
        print(
            f"{r['model']:<32} {r['fc_pass']}/{r['fc_total']:>8} "
            f"{r['copy_len_ok']}/{r['copy_total']:>6} {ttft:>10}"
        )
    print(
        "\nПравило выбора: бери САМУЮ ДЕШЁВУЮ модель, что проходит function calling на денежном пути."
    )
    print(
        "Скорость (TTFT) — отдельная ось: parsing ждёт пользователь. Если разница в колонке TTFT "
        "ощутима — поставь быстрый эндпоинт через OPENROUTER_PARSING_PROVIDER_SORT (=throughput/latency) "
        "или смени LLM_PARSING на более быструю tool-use модель."
    )
    print("Качество русского в текстах оцени глазами по сэмплам — его автоскоринг не ловит.")
    print(
        "Можно разные модели: дешёвую на парсинг (MODEL_PARSING), посильнее на копирайт (MODEL_COPY)."
    )


if __name__ == "__main__":
    asyncio.run(main())
