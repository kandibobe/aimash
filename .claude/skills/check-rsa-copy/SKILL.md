---
name: check-rsa-copy
description: Валидация длины рекламных текстов RSA (Responsive Search Ads) для Google Ads с правильным подсчётом кириллицы. Использовать при генерации/проверке заголовков и описаний.
---

# Валидация длины RSA-текстов

Лимиты Google Ads (Responsive Search Ads):
- **Headline (заголовок): ≤ 30 символов**, до 15 шт.
- **Description (описание): ≤ 90 символов**, до 4 шт.
- **Display path: ≤ 15 символов**, 2 сегмента.

## КЛЮЧЕВОЕ правило про кириллицу
Двойную ширину Google считает **только** для CJK (китайский/японский/корейский). **Кириллица = 1 символ**, как латиница. Полный лимит 30/90/15 действует для русского/украинского — НЕ урезать.

Считать по **Unicode code points** (`len(str)` в Python), а НЕ по UTF-8 байтам (иначе кириллица посчитается вдвое).

```python
def rsa_len(text: str) -> int:
    # Кириллица и латиница = 1; только CJK = 2.
    def width(ch: str) -> int:
        o = ord(ch)
        cjk = (
            0x4E00 <= o <= 0x9FFF or  # CJK Unified
            0x3040 <= o <= 0x30FF or  # Hiragana/Katakana
            0xAC00 <= o <= 0xD7A3     # Hangul
        )
        return 2 if cjk else 1
    return sum(width(c) for c in text)

LIMITS = {"headline": 30, "description": 90, "path": 15}

def validate(text: str, kind: str) -> tuple[bool, int]:
    n = rsa_len(text)
    return n <= LIMITS[kind], n
```

## Правила генерации (для прохождения модерации)
- Без CAPS LOCK, без лишней пунктуации, без спам-символов.
- CTA, уникальность, ключевые слова в тексте.
- Длину считает **КОД после генерации**; если превышено — перегенерировать конкретный элемент, не доверять подсчёт модели.

## Чеклист
- [ ] подсчёт по code points, не по байтам
- [ ] кириллица = 1 символ
- [ ] headline ≤30 / description ≤90 / path ≤15
- [ ] нет CAPS/спам-символов
- [ ] валидация в коде, не на доверии к модели
