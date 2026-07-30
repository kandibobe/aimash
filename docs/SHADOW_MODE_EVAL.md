# Shadow Mode Evaluation

Текущий shadow-контур — `scheduler/rollback.py` + `rollback_watch`. Он наблюдает только за успешно
применёнными и обратимыми `update_budget`, `update_bid`, `update_keyword_bid` с вычислимым
`expected_ratio`.

В первые 3–6 часов честно измеримы расход и клики, но не CPA: конверсии продолжают досчитываться.
Поэтому detector сравнивает `cost_micros`, а не заявляет «CPA ухудшился». База — медиана и MAD того
же часа того же дня недели за четыре предыдущие недели, минимум три непустых samples. База
масштабируется на ожидаемый эффект «было → станет», иначе detector ловил бы собственную причину.

Вердикты: `ok`, `degraded`, `insufficient`. Недостаток данных никогда не повышается до degraded.
В режиме `shadow` строка только получает verdict; уведомлений и мутаций нет. `alert` отправляет
человеку диагноз, но обратный proposal рождается в новом человеческом ходе. `auto` сейчас
намеренно деградирует в shadow и не является реализованным денежным путём.

Перед любым auto-cutover нужна размеченная выборка shadow verdicts и минимум:

- precision/false-positive rate отдельно по operation и account;
- достаточное число окон с baseline и доля `insufficient`;
- ручная проверка, что expected ratio корректен;
- rollback proposal с полным обычным confirm/freshness/account/audit контуром;
- staged rollout только на Draft/test, затем per-account opt-in и kill-switch.

Проверки: `tests/test_rollback.py` и `tests/test_rollback_watch.py`.
