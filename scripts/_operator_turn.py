"""Единственная точка, где скрипт объявляет ход человеческим (Волна 1.4, второй бит провенанса).

Зачем вообще. `human_turn` поднимает бит `origin_human_turn`, без которого `_require_user_command`
отклоняет любую денежную операцию. У скриптов из `scripts/` этого бита не было ни у одного, и с
da101fd они молча сломались: `exec_demo*.py` и `live_smoke_*.py` гоняют бюджет/ставки/создание
кампаний, то есть ровно те 8 call-site'ов, что гейт и стережёт. Живая проверка прода (`/verify-live`)
опиралась на путь, который перестал доезжать до SDK.

Почему это честно, а не байпас. Бит отделяет «человек попросил» от «машина решила». Скрипт, который
оператор запустил руками из консоли, — человеческий ход по определению: в этом процессе нет агента,
способного решить самому, а набор операций зашит в код и не приходит из модели.

Почему хелпер один, а не `human_turn` в девяти файлах. Мета-гард
`tests/test_provenance_gate.test_human_turn_call_sites_are_allow_listed` держит список входов,
поднимающих бит, — и раньше не видел `scripts/` вовсе. Одна точка = одна строка в allow-list, и
новый вход виден в диффе, а не растворяется среди девяти копий.

Fail-closed на реальном опасном сценарии: не «кто-то запустит скрипт из cron» (это по-прежнему
человеческое решение, а `exec_demo*` вдобавок закрыты `require_dev_env`), а «кто-то переиспользует
удобный контекст-менеджер ВНУТРИ сервера» — в хендлере, MCP-инструменте, scheduler-джобе. Там бит
поднимать нельзя ни при каких условиях, и такой вызов здесь отказывает: точка входа процесса
(`sys.argv[0]`) обязана лежать в `scripts/`. У `python -m bot.main`, `python -m mcp_server` и pytest
она лежит в другом месте.
"""

from __future__ import annotations

import pathlib
import sys
from contextlib import contextmanager
from typing import Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.provenance import human_turn  # noqa: E402

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent


@contextmanager
def operator_turn(*, actor_user_id: int | None = None) -> Iterator[None]:
    """Ход запустил живой оператор из консоли. Открывать ТОЛЬКО в `scripts/` и только вокруг
    создания черновика — не вокруг всего `main()`, чтобы область действия бита была видна глазом."""
    entry = pathlib.Path(sys.argv[0] or ".").resolve()
    if entry.parent != _SCRIPTS_DIR:
        raise RuntimeError(
            f"operator_turn открыт не из scripts/ (точка входа: {entry.name}). Человеческий бит "
            "внутри сервера поднимает только доверенный слой Telegram — см. core/provenance.py."
        )
    with human_turn(actor_user_id=actor_user_id):
        yield
