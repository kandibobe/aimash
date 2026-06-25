"""A/B-тест моделей (Фаза −1) — выбираем модель ПО ДАННЫМ, не по бренду.

Прогоняет реальные русские команды через несколько моделей (DeepSeek / Hermes / Claude)
через OpenRouter и проверяет:
  1) function calling: выбрана ли правильная функция и аргументы (ловушки «на 20%» vs «до 20%»,
     суммы/валюта, типы соответствия broad/phrase/exact, неоднозначное → должна УТОЧНИТЬ);
  2) генерация RSA-текстов: укладывается ли в лимиты (кириллица = 1 символ), без CAPS.

Запуск:  python scripts/ab_test_models.py
Нужен только OPENROUTER_API_KEY в .env. Печатает таблицу: автоматический скоринг +
сэмплы для ручной оценки качества русского (его автоматом не оценить).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI  # noqa: E402

from core.config import settings  # noqa: E402

CANDIDATES = [
    "deepseek/deepseek-chat",
    "nousresearch/hermes-4-70b",
    "nousresearch/hermes-4-405b",
    "anthropic/claude-sonnet-4.6",
]

SYSTEM = (
    "Ты — исполнитель команд для Google Ads. По команде пользователя вызови ПОДХОДЯЩУЮ функцию "
    "с точными аргументами. Различай 'на N%' (изменить НА процент) и 'до N' (установить В значение). "
    "Если команда неоднозначна (не указана кампания, сумма или направление) — вызови ask_clarification, "
    "НЕ угадывай. Ничего не выполняй, только предложи вызов функции."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_budget",
            "description": "Изменить дневной бюджет кампании.",
            "parameters": {
                "type": "object",
                "properties": {
                    "campaign": {"type": "string"},
                    "mode": {"type": "string", "enum": ["increase_by_percent", "increase_by_amount", "set_to"]},
                    "value": {"type": "number"},
                    "currency": {"type": "string", "enum": ["USD", "UAH", "EUR", "percent"]},
                },
                "required": ["campaign", "mode", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_bid",
            "description": "Изменить ставку.",
            "parameters": {
                "type": "object",
                "properties": {
                    "campaign": {"type": "string"},
                    "mode": {"type": "string", "enum": ["increase_by_percent", "set_to"]},
                    "value": {"type": "number"},
                    "currency": {"type": "string", "enum": ["USD", "UAH", "EUR", "percent"]},
                },
                "required": ["mode", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_keywords",
            "description": "Добавить ключевые слова с типом соответствия.",
            "parameters": {
                "type": "object",
                "properties": {
                    "campaign": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "match_type": {"type": "string", "enum": ["broad", "phrase", "exact"]},
                },
                "required": ["keywords", "match_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_negative_keywords",
            "description": "Добавить минус-слова.",
            "parameters": {
                "type": "object",
                "properties": {"keywords": {"type": "array", "items": {"type": "string"}}},
                "required": ["keywords"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_campaign",
            "description": "Поставить кампанию на паузу.",
            "parameters": {
                "type": "object",
                "properties": {"campaign": {"type": "string"}},
                "required": ["campaign"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_geo_proximity",
            "description": "Таргетинг по точке с радиусом.",
            "parameters": {
                "type": "object",
                "properties": {
                    "campaign": {"type": "string"},
                    "location": {"type": "string"},
                    "radius_km": {"type": "number"},
                },
                "required": ["location", "radius_km"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "Прочитать статистику (read-only).",
            "parameters": {
                "type": "object",
                "properties": {"account": {"type": "string"}, "period_days": {"type": "integer"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Команда неоднозначна — переспросить пользователя, НЕ угадывать.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]

# (команда, ожидаемая функция, проверка аргументов, комментарий)
SCENARIOS = [
    ("повысь бюджет кампании Лето на 20%", "update_budget",
     lambda a: a.get("mode") == "increase_by_percent" and a.get("value") == 20,
     "на 20% = increase_by_percent, не set_to"),
    ("смени бюджет кампании X на $50 в день", "update_budget",
     lambda a: a.get("mode") in ("set_to", "increase_by_amount") and a.get("value") == 50 and a.get("currency") == "USD",
     "$50 в день, валюта USD"),
    ("подними ставку до 10 грн", "update_bid",
     lambda a: a.get("mode") == "set_to" and a.get("value") == 10 and a.get("currency") == "UAH",
     "'до 10' = set_to, грн = UAH"),
    ("добавь ключи: купить телефон, цена телефона — фразовое соответствие", "add_keywords",
     lambda a: a.get("match_type") == "phrase" and len(a.get("keywords", [])) == 2,
     "фразовое = phrase, 2 ключа"),
    ("добавь точное соответствие: ремонт обуви киев", "add_keywords",
     lambda a: a.get("match_type") == "exact",
     "точное = exact"),
    ("поставь на паузу кампанию Зима", "pause_campaign",
     lambda a: "Зима" in (a.get("campaign") or ""),
     "пауза кампании"),
    ("добавь минус-слово бесплатно", "add_negative_keywords",
     lambda a: any("бесплатно" in k for k in a.get("keywords", [])),
     "минус-слово"),
    ("измени ГЕО на Киев + радиус 10 км", "set_geo_proximity",
     lambda a: a.get("radius_km") == 10 and "иев" in (a.get("location") or "").lower(),
     "радиус 10 км, Киев"),
    ("покажи статистику по аккаунту 123 за последние 30 дней", "get_stats",
     lambda a: a.get("period_days") == 30,
     "read-only, 30 дней"),
    ("увеличь бюджет", "ask_clarification",
     lambda a: True,
     "НЕОДНОЗНАЧНО → должен уточнить, не угадывать"),
    ("повысь ставку на 15% в кампании Осень", "update_bid",
     lambda a: a.get("mode") == "increase_by_percent" and a.get("value") == 15,
     "на 15% = increase_by_percent"),
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
    if not settings.openrouter_api_key:
        print("❌ OPENROUTER_API_KEY не задан в .env — добавь ключ и перезапусти.")
        sys.exit(1)
    return AsyncOpenAI(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url)


async def run_scenario(cli: AsyncOpenAI, model: str, command: str) -> tuple[str, dict]:
    resp = await cli.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": command}],
        tools=TOOLS,
        tool_choice="auto",
    )
    msg = resp.choices[0].message
    if not msg.tool_calls:
        return "(no tool call)", {}
    call = msg.tool_calls[0]
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    return call.function.name, args


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


async def test_model(cli: AsyncOpenAI, model: str) -> dict:
    print(f"\n=== {model} ===")
    fc_pass = 0
    for command, expected_fn, check, note in SCENARIOS:
        try:
            fn, args = await run_scenario(cli, model, command)
        except Exception as e:  # модель/сеть
            print(f"  ✗ ERROR  «{command}» → {type(e).__name__}: {e}")
            continue
        ok = fn == expected_fn and (check(args) if fn == expected_fn else False)
        fc_pass += int(ok)
        mark = "✓" if ok else "✗"
        print(f"  {mark} «{command}» → {fn} {args}  [{note}]")

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
    print(f"{'модель':<32} {'функции':>10} {'длина текстов':>16}")
    for r in sorted(results, key=lambda x: -x["fc_pass"]):
        print(f"{r['model']:<32} {r['fc_pass']}/{r['fc_total']:>8} {r['copy_len_ok']}/{r['copy_total']:>12}")
    print("\nПравило выбора: бери САМУЮ ДЕШЁВУЮ модель, что проходит function calling на денежном пути.")
    print("Качество русского в текстах оцени глазами по сэмплам — его автоскоринг не ловит.")
    print("Можно разные модели: дешёвую на парсинг (MODEL_PARSING), посильнее на копирайт (MODEL_COPY).")


if __name__ == "__main__":
    asyncio.run(main())
