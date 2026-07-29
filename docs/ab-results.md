# A/B-тест моделей — результат (Фаза −1)

> ⚠️ **Читать как замер, не как действующее решение.** Раздел «Решение» ниже — состояние Фазы −1
> (выбран `deepseek/deepseek-chat`). **Действующая раскладка моделей по ролям — `SPEC.md` §10.1**,
> и она другая. Живым из этого файла остаётся то, что здесь измерено:
> **Hermes-модели дали 0/11 function-calling** через OpenRouter — именно на этом стоит выбор
> модели-мозга гейтвея (`docs/TZ-Aimash-Hermes-Agent.md`, `docs/AUDIT-open-source.md`).
> Слаги проверяются против живого каталога OpenRouter в `tests/test_model_slugs.py`.

Прогон `scripts/ab_test_models.py` через OpenRouter на реальных русских командах + генерации RSA-текстов. Решение по данным, не по бренду.

| Модель | Function calling | RSA-длина | Цена /1M (in/out) | Вывод |
|---|---|---|---|---|
| anthropic/claude-sonnet-4.6 | 9/11 | 7/7 | ~$3 / $15 | топ-надёжность, дорого |
| **deepseek/deepseek-chat** | 8–9/11 | 7/7 | ~$0.23 / $0.34 | **≈Claude, в ~13× дешевле → выбрано** |
| nousresearch/hermes-4-70b | 0/11 | 7/7 | ~$0.13 / $0.40 | ❌ нет tool use на OpenRouter |
| nousresearch/hermes-4-405b | 0/11 | 5/7 | ~$1 / $3 | ❌ нет tool use + слабее русский |

## Решение
- **Парсинг команд (денежный путь): `deepseek/deepseek-chat`** — почти как Claude, кратно дешевле.
- **Копирайт: `deepseek/deepseek-chat`** — сэмплы нативные, в лимитах; апгрейд на Claude Sonnet при желании заказчика.
- **Fallback: `anthropic/claude-sonnet-4.6`** — топ-надёжность.
- **Hermes — выбыл** с function-calling пути: провайдеры OpenRouter не дают tool use («No endpoints found that support tool use»). Объективная причина, не предвзятость.

## Замечания по прогону
- Обе топ-модели корректно **переспрашивают**, когда кампания не указана (безопасное поведение) — мой rubric местами это штрафовал, поэтому реальное качество DeepSeek/Claude выше «8–9/11».
- В чек-ГЕО был баг (`"ив"` вместо `"иев"`) — исправлен; DeepSeek на ГЕО отвечал корректно.
- Качество русского в текстах оценивается глазами по сэмплам (автоскоринг ловит только длину). DeepSeek и Claude — нативно; Hermes-405b давал артефакты.

## Конфиг (закреплено в .env)
```
MODEL_PARSING=deepseek/deepseek-chat
MODEL_COPY=deepseek/deepseek-chat
MODEL_FALLBACK=anthropic/claude-sonnet-4.6
```
Модель остаётся **сменяемой** — перепрогнать тест и поменять строку конфига можно в любой момент.
