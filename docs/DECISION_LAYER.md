# Decision / Action Queue

## Граница

`operations/` — операционная нервная система поверх существующего ядра. Она обнаруживает,
дедуплицирует, приоритизирует и объясняет, но **не даёт права на Google Ads mutation**.

```
signal → operational_decisions / ops_incidents → человек решает
       → отдельный proposal «было → станет» → reply-confirm → claim → Ads → audit
```

`approved` у decision означает «рекомендация принята в работу». Это не `confirmed` у proposal.
`applied` допустим только через `mark_decision_applied_from_audit`: один correlated UPDATE требует
matching proposal и `audit_log` со статусом `applied`, тем же customer и совместимой operation.
Произвольная ссылка на `proposal_confirmation_id` статус не меняет.

## Контракты

| Контур | Код | Персистентность | Что гарантирует |
|---|---|---|---|
| Decision queue | `operations/decisions.py` | `operational_decisions` | UNIQUE active fingerprint, audit-proven applied, evidence, confidence, assignee, lifecycle |
| Incidents | `operations/incidents.py` | `ops_incidents` | один incident на поток событий, ACK/snooze/resolve, cooldown/escalation |
| Pacing | `operations/pacing.py` | `budget_plans`, `pacing_snapshots` | transactional unique versions, CSV/Sheets, plan/fact/projection/ceilings; только advisory |
| Integrity | `operations/integrity.py` | через decision | primary actions, zero/drop, clicks↔sessions, Ads↔CRM; `None` не трактуется как ноль |
| Change correlation | `operations/correlation.py` | в evidence | internal audit + Google `change_event`; correlation явно не называется причиной |
| Search mining | `operations/search_mining.py` | через decision/artifact | harvest, waste n-grams, conflicts, cannibalization, MCC themes |
| Creative/LP QA | `operations/qa.py` | через decision | лимиты из `adcopy.validate`, policy/coverage, SSRF-safe GET; форма не отправляется |
| Experiments | `operations/experiments.py` | `managed_experiments` | hypothesis, control, treatment, KPI, sample/window, keep/rollback/inconclusive |
| Playbooks | `operations/playbooks.py` | `playbook_versions` | только `decision`/`incident`; ключи mutation/execute запрещены валидатором |
| Bulk guardrails | `operations/policy.py`, `operations/bulk.py` | proposal создаёт вызывающий | dry-run, полный scope diff, cap targets/delta; прямого execute нет |
| Governance | `operations/governance.py` | roles + votes | RBAC и независимый vote; four-eyes добавлен внутрь claim-CAS |
| Revenue/portfolio | `operations/revenue.py`, `operations/portfolio.py` | revenue/channel snapshots | keyed HMAC CRM ids, explicit customer scope, no cross-client reallocation |
| Routing | `operations/routing.py`, `operations/outbox.py` | `notification_routes`, `notification_outbox` | config refs без secret values; atomic enqueue, lease, retry/dead-letter; payload редактируется |
| Identity | `operations/identity.py` | `external_identities` | только verified claims; issuer/subject проходят domain-separated keyed HMAC |
| Explainability | `operations/explain.py` | ответ | applied/failed нельзя заявить без audit reference |

## Lifecycle

Decision: `new → acknowledged|approved|rejected|snoozed`; `snoozed → new`; `approved → applied`;
активная строка может стать `expired`. Incident: `open → acknowledged|snoozed|resolved`; новый факт
переоткрывает resolved incident и увеличивает `occurrence_count`.

## Доставка escalation

Scheduler не отмечает incident доставленным перед сетевым вызовом. `enqueue_due_escalations`
одной транзакцией сдвигает escalation cursor и создаёт по outbox-row на каждый effective route;
customer-specific route подавляет идентичный global route. Воркеры забирают строки атомарным
lease-CAS, после сбоя повторяют с exponential backoff и после ограниченного числа попыток переводят
в `dead`. Resolve/snooze отменяет ещё не взятые строки как `cancelled`; claim повторно проверяет,
что incident активен. В БД сохраняется только тип исключения, не `str(e)`.

Семантика — **at-least-once**: падение после успешного send, но до `delivered` commit может дать
дубль. Отметить успех до send означало бы необратимую тихую потерю, поэтому это запрещено.
Стабильный `dedup_key` передаётся transport adapter и позволяет каналам с идемпотентностью убрать
дубль. Telegram такого примитива не даёт. `destination_ref` разрешается из env только в момент
отправки и никогда не попадает в outbox, логи или текст ошибки.

## Four-eyes

`FOUR_EYES_REQUIRED=false` сохраняет прежний flow. При `true` proposal выбранного тира
(`FOUR_EYES_RISK_TIERS_CSV`, дефолт `L3`) проходит `ConfirmStore.claim` только если в **том же SQL
UPDATE** существует approve от активного Approver/Admin нужного customer, автор известен и vote
принадлежит другому user. Независимый reject блокирует claim и остаётся блокирующим после отзыва
роли rejector. Пустой runtime-набор тиров при включённом флаге блокирует claim, а неизвестный тир
роняет конфигурацию. Reply-confirm автора, TTL, account lock,
provenance, 2FA, freshness и audit остаются обязательны.

## Что ещё требует live-cutover

Кодовая MCP-поверхность публикует 38 READ + 1 META + 53 agent-first PLAN/state + 1 WRITE при включённом
`HERMES_WRITE_ENABLED`: `list_decisions`, `update_decision`, `list_incidents`, `update_incident`
проходят через тот же HMAC trusted-turn wrapper и per-account RBAC. Переходы статусов атомарны и
дополнительно привязаны к `customer_id`; они управляют операционным состоянием, но не выполняют Google
Ads mutation. `approved` остаётся только решением оператора — Ads-изменение по-прежнему требует
отдельного proposal, reply-якоря, CAS и audit-row.

Код и схема не равны включённому продукту: до завершения live deploy/surface probe этот реестр нельзя
считать принятым на проде.
Живой scheduler подключает Telegram adapter;
Slack/email/Teams требуют настроенных transport adapters и uppercase config refs
(`SLACK_OPS_CHANNEL`), не raw URL/token. Перед transport title/body проходят `redact_text`. SSO
требует проверки подписи/issuer/audience в доверенном gateway — модуль
identity намеренно не принимает сырой токен. Meta/Microsoft/TikTok требуют ingestion adapters и
credentials; cross-channel рекомендации не исполняются.

## Каналы-источники и рендеринг

Monitoring, audit, waste mining и shadow evaluation создают тот же нормализованный объект, а не
свои prose-alerts. Morning Standup группирует decision по аккаунту, Hourly Watchdog показывает
только срочные, Weekly Janitor — waste-находки. В Telegram каждая карточка обязана показать:
что случилось, severity, evidence/why, рекомендуемое и запрещённое действие, confidence.

Приёмка ядра: `python -m pytest -q tests/test_operations_layer.py tests/test_notification_outbox.py`
и полный `python -m pytest -q`.
