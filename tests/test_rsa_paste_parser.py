"""§10 list-UX: разбор присланного менеджером СПИСКА заголовков/описаний обратно в наборы,
и round-trip через fmt_rsa_list_block → parse_rsa_paste."""

from __future__ import annotations

from types import SimpleNamespace

from core.texts import fmt_rsa_list_block, parse_rsa_paste


def _sess(headlines: list[str], descriptions: list[str]):
    return SimpleNamespace(
        headlines=[{"text": t} for t in headlines],
        descriptions=[{"text": t} for t in descriptions],
    )


def test_parse_with_section_headers_and_numbering_and_annotations():
    text = (
        "ЗАГОЛОВКИ (≤30):\n"
        "1. Купить окна недорого  [18/30]\n"
        "2. Окна ПВХ в Киеве  [15/30]\n"
        "3. Установка окон  [15/30]\n"
        "\n"
        "ОПИСАНИЯ (≤90):\n"
        "1. Качественные окна с гарантией.  [30/90]\n"
        "2. Замер и доставка бесплатно.  [26/90]\n"
    )
    h, d = parse_rsa_paste(text)
    assert h == ["Купить окна недорого", "Окна ПВХ в Киеве", "Установка окон"]
    assert d == ["Качественные окна с гарантией.", "Замер и доставка бесплатно."]


def test_parse_without_headers_splits_on_blank_line():
    text = "Заголовок один\nЗаголовок два\nЗаголовок три\n\nОписание один\nОписание два"
    h, d = parse_rsa_paste(text)
    assert h == ["Заголовок один", "Заголовок два", "Заголовок три"]
    assert d == ["Описание один", "Описание два"]


def test_parse_strips_quotes_and_paren_numbering():
    text = 'HEADLINES:\n1) «Заголовок»\n2) "Второй"\nDESCRIPTIONS:\n1) Описание'
    h, d = parse_rsa_paste(text)
    assert h == ["Заголовок", "Второй"]
    assert d == ["Описание"]


def test_round_trip_render_then_parse_recovers_texts():
    headlines = ["Купить окна", "Окна ПВХ Киев", "Установка окон под ключ"]
    descriptions = ["Гарантия 5 лет на все окна.", "Бесплатный замер и доставка."]
    block = fmt_rsa_list_block(_sess(headlines, descriptions), lang="ru")
    h, d = parse_rsa_paste(block)
    assert h == headlines
    assert d == descriptions


def test_empty_input_yields_empty_lists():
    h, d = parse_rsa_paste("")
    assert h == [] and d == []
