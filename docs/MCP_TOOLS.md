# HERMES 3.0 MCP Tool Registry

FastMCP server публикует строго проверяемую базовую поверхность из **86 инструментов**.
При `RESEARCH_ARCHIVE_ENABLED=true` к ней добавляются два research archive tools, и фактический total равен **88**. Источник истины —
Python registries в [`mcp_server/`](../mcp_server/), а не ручной список: при старте
`require_registered_surface()` сравнивает фактически зарегистрированные имена с ожидаемым набором и
останавливает процесс при drift.

## Состав поверхности

| Surface | Registry | Count |
|---|---|---:|
| READ | `READ_TOOL_FUNCS` | 26 |
| META | `META_TOOL_FUNCS` | 1 |
| PLAN & STATE | `PLAN_STATE_TOOL_FUNCS` | 15 |
| ACTION | `ACTION_TOOL_FUNCS` | 42 |
| Composite proposal | `COMPOSITE_TOOL_FUNCS` | 1 |
| Approval execution | `EXECUTE_TOOL_FUNCS` | 1 |
| **Total** | exact union | **86** |
| Optional research archive | `RESEARCH_TOOL_FUNCS` | **+2** |

В составе поверхности — **26 READ-инструментов**, 1 META, 15 PLAN & STATE, 42 ACTION, один
`composite_change` и один `execute_confirmed`.

```python
expected = (
    READ_MCP_TOOLS
    | META_MCP_TOOLS
    | PLAN_STATE_MCP_TOOLS
    | ACTION_MCP_TOOLS
    | COMPOSITE_MCP_TOOLS
    | EXECUTE_MCP_TOOLS
)
assert len(expected) == 86
# settings.research_archive_enabled -> len(expected | RESEARCH_MCP_TOOLS) == 88
```

## READ — статистика, аудит и подготовка

READ tools проходят account/read ceiling и возвращают структурированные envelopes. Они не создают
proposal и не вызывают Google Ads mutate methods.

| Группа | Tools | Назначение |
|---|---|---|
| Accounts & MCC | `list_accounts`, `get_mcc_summary`, `get_mcc_deep`, `build_mcc_report`, `get_quota` | Discovery, агрегаты MCC, единый XLSX, quota |
| GAQL & changes | `execute_google_ads_query`, `get_account_changes`, `get_change_history` | Typed GAQL и история изменений |
| Audit & analysis | `get_account_audit`, `analyze_account` | Детерминированные проверки и аналитический пакет |
| Aimash Memory reads | `recall_client`, `get_client_card`, `list_client_facts_structured`, `get_crawl_status`, `list_site_pages` | Account-scoped профиль, досье, crawl state |
| Keywords | `keyword_ideas`, `seed_keywords`, `parse_keywords_input`, `filter_keyword_relevance`, `cluster_keywords`, `suggest_negatives`, `export_keyword_report` | Исследование и подготовка семантики |
| Creative & reports | `generate_rsa`, `validate_adcopy`, `build_display_path`, `build_report` | RSA, deterministic validation и artifacts |

### Self-healing envelope

```json
{
  "ok": false,
  "error_code": "invalid_argument",
  "error_type": "RECOVERABLE",
  "message": "Safe, redacted explanation",
  "suggested_action": "Refresh state or correct the typed argument"
}
```

Hermes читает `error_code`, `message` и `suggested_action`, добавляет новое evidence и повторяет
вызов. Сырые exception и секреты в MCP response не возвращаются.

## META

`get_bridge_capabilities` сообщает возможности trusted bridge. META не является READ Google Ads и
не даёт права на mutation.

## PLAN & STATE — proposals, incidents и decisions

Эти 15 tools доступны только через trusted human context. Они управляют durable state, но сами не
исполняют подтверждённую Google Ads mutation.

| Группа | Tools | Поведение |
|---|---|---|
| Proposals | `list_pending_proposals`, `cancel_proposal` | Показывает только proposals текущего actor/chat; отмена — CAS в `rejected` |
| Decisions | `list_decisions`, `update_decision` | Очередь advisory decisions и атомарные state transitions |
| Incidents | `list_incidents`, `update_incident` | Deduplicated incidents, ACK/snooze/resolve/reopen |
| Keyword workflows | `start_keyword_research`, `read_keyword_sheet`, `create_search_term_review`, `read_search_term_review` | Durable research/review round-trip без Ads mutation |
| Artifacts & media | `build_monthly_pdf`, `ingest_media` | Проверяемые artifacts и trusted inbound media |
| Aimash Memory | `profile_change`, `profile_clear`, `start_client_crawl` | Создаёт account-scoped memory proposal |

`PLAN_MCP_TOOLS` намеренно не содержит `execute_confirmed`.

## 42 ACTION tools

ACTION tools имеют прямые agent-first имена. Внутренние функции могут называться `propose_*`, но
публичная MCP-поверхность не заставляет Hermes выбирать между искусственными wizard phases.

Каждый ACTION сначала:

1. валидирует typed payload;
2. проверяет trusted human provenance и account context;
3. снимает attested `before` state;
4. сохраняет один `pending` proposal с полным preview;
5. применяет `confirm/policy.py`: возвращает `APPROVAL_REQUIRED` либо безопасно продолжает через тот
   же CAS execution path, если операция разрешена без отдельной карточки.

| Домен | Count | ACTION tools |
|---|---:|---|
| Budget & bidding | 4 | `update_budget`, `update_bid`, `update_keyword_bid`, `set_bidding_strategy` |
| Campaign lifecycle/settings | 8 | `pause_campaign`, `resume_campaign`, `launch_campaign`, `remove_campaign`, `update_campaign`, `set_campaign_network`, `set_campaign_display_network`, `set_campaign_geo_target_type` |
| Ad group & ad lifecycle | 6 | `pause_ad_group`, `resume_ad_group`, `remove_ad_group`, `pause_ad`, `resume_ad`, `remove_ad` |
| Keywords & shared negatives | 6 | `add_keywords`, `remove_keywords`, `add_negative_keywords`, `remove_negative_keywords`, `add_negatives_to_shared_set`, `attach_shared_set` |
| Geo & audiences | 4 | `set_geo_location`, `set_geo_proximity`, `attach_audience`, `detach_audience` |
| Campaign creation | 5 | `create_search_campaign`, `create_gdn_campaign`, `create_demand_gen_campaign`, `create_video_campaign`, `create_app_campaign` |
| Ads & assets | 9 | `create_rsa`, `add_sitelinks`, `add_callouts`, `add_structured_snippets`, `attach_image_asset`, `add_call_asset`, `add_promotion`, `add_price_asset`, `remove_asset_link` |
| **Total** | **42** | Exact equality with `MUTATION_TOOLS` is test-guarded |

`composite_change` не является 43-й mutation: он принимает 2–10 rollbackable ACTION operations,
снимает их `before` state и сохраняет ровно один parent proposal. Исполнение остаётся только через
trusted reply и `execute_confirmed`; частично выполненный пакет проходит компенсацию и post-verify.

## `execute_confirmed`

`execute_confirmed` — единственная approval execution entrypoint. У инструмента нет model-supplied
`account` или `confirmation_id`:

```text
execute_confirmed()
```

Trusted Transport поставляет marker, actor, chat и reply anchor. Затем код выполняет:

```text
verified reply
  -> pending --CAS--> confirmed
  -> account + freshness + policy gates
  -> confirmed --CAS--> executing
  -> typed Google Ads or Aimash Memory executor
  -> executing --CAS--> applied + audit
  -> post-verify/readback
  -> structured result
```

Повторный reply не может повторить mutation: после первого claim строка больше не имеет
`status=confirmed`. Если audit result недоступен, tool возвращает `needs_review`, а не выдумывает
успешное сообщение.

## MEMORY — cross-cutting surface

MEMORY не добавляет инструменты сверх 86; это функциональная группа внутри READ и PLAN & STATE.

| Операция | Surface | Mutation semantics |
|---|---|---|
| `recall_client` | READ | PII-safe account context для стратегии и генераторов |
| `get_client_card` | READ | Карточка профиля/досье |
| `list_client_facts_structured` | READ | Структурированные факты клиента |
| `get_crawl_status` | READ | Наблюдение за crawl job |
| `list_site_pages` | READ | Сохранённая карта сайта |
| `profile_change` | PLAN & STATE | `pending` save/update proposal |
| `profile_clear` | PLAN & STATE | `pending` clear proposal |
| `start_client_crawl` | PLAN & STATE | Crawl + draft dossier + один memory proposal |
| `execute_confirmed` | Approval execution | Применяет подтверждённый memory proposal через отдельный typed executor |

Google Ads и Aimash Memory используют общий CAS/audit contract, но memory executor не вызывает
Google Ads SDK.

## Проверка registry

```bash
python - <<'PY'
from mcp_server.tools_meta import META_TOOL_FUNCS
from mcp_server.tools_plan import PLAN_STATE_TOOL_FUNCS
from mcp_server.tools_read import READ_TOOL_FUNCS
from mcp_server.tools_write import ACTION_TOOL_FUNCS, COMPOSITE_TOOL_FUNCS, EXECUTE_TOOL_FUNCS

parts = {
    "READ": len(READ_TOOL_FUNCS),
    "META": len(META_TOOL_FUNCS),
    "PLAN_STATE": len(PLAN_STATE_TOOL_FUNCS),
    "ACTIONS": len(ACTION_TOOL_FUNCS),
    "COMPOSITE": len(COMPOSITE_TOOL_FUNCS),
    "EXECUTE": len(EXECUTE_TOOL_FUNCS),
}
print(parts)
assert parts == {"READ": 26, "META": 1, "PLAN_STATE": 15, "ACTIONS": 42, "COMPOSITE": 1, "EXECUTE": 1}
PY
```
