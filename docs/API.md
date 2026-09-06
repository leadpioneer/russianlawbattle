# Справочник API бэкенда

FastAPI-приложение `src/api.py`. База: `http://127.0.0.1:8000` (конфигурируется
`NEXT_PUBLIC_API_BASE` на фронтенде). Все ответы — JSON, кроме скачивания отчёта.
Ошибки FastAPI — `{"detail": "человекочитаемое сообщение"}`.

## REST

### `GET /api/health`
Живость сервера. → `{"status": "ok"}`. Используется индикатором фронтенда и install.bat.

### `GET /api/defaults`
Преднастройки из `config.yaml` + `.env` (для умной формы). Полный ключ не отдаётся —
только маска.

```json
{
  "config_found": true,
  "base_url": "https://routerai.ru/api/v1",
  "model_claimant_lawyer": "~z-ai/glm-flash-latest",
  "model_defendant_lawyer": "~z-ai/glm-flash-latest",
  "model_judge": "~z-ai/glm-flash-latest",
  "jurisdiction": "Российская Федерация, гражданское право, ЗоПП",
  "max_rounds": 6,
  "max_context_tokens": 950000,
  "llm_params": {"reasoning": {"effort": "low"}},
  "api_key_env": "ROUTERAI_API_KEY",
  "has_env_key": true,
  "api_key_masked": "sk-pvG…i-nH"
}
```

### `POST /api/setup`
Создать сессию. Тело:

```json
{
  "base_url": "https://routerai.ru/api/v1",
  "api_key": "",
  "model_claimant_lawyer": "~z-ai/glm-flash-latest",
  "model_defendant_lawyer": "~z-ai/glm-flash-latest",
  "model_judge": "~z-ai/glm-flash-latest",
  "jurisdiction": "Российская Федерация, гражданское право",
  "max_rounds": 2,
  "max_context_tokens": 12000,
  "target_side": "claimant",
  "llm_params": {"reasoning": {"effort": "low"}}
}
```

Поля: `api_key` пустой = взять из `.env` (по `api_key_env` из config.yaml); `max_rounds`
1..10; `max_context_tokens` 1_000..2_000_000; `target_side` = `claimant|defendant`;
`llm_params` передаются роутеру «как есть» через `extra_body`.

→ `{"session_id": "hex12", "status": "created", "target_side": "claimant",
"api_key_source": "env:ROUTERAI_API_KEY"}`.
Ошибки: `400` — ключ не задан ни в форме, ни в `.env`; `422` — ошибка валидации pydantic.

### `POST /api/upload/{session_id}`
Multipart: `files[]` (PDF/DOCX/TXT/MD) + поле `context` (текст, может быть пустым).
Файлы пишутся в `sessions/<id>/case_files/`, контекст — в `case_context.md`.
→ `{"saved_files": [...], "rejected_files": [{"file", "reason"}], "context_saved": bool,
"status": "ready"}`.
Ошибки: `404` — сессии нет; `409` — симуляция уже идёт.

### `POST /api/run/{session_id}`
Запуск симуляции в фоновом потоке (ответ приходит сразу). Перед стартом: если
`case_context.md` нет, но файлы есть — генерирует служебный контекст из перечня
документов; подгружает прайсы роутера (`fetch_model_pricing`) для оценки денег.
→ `{"session_id": "…", "status": "running"}`.
Ошибки: `400` — ключ не задан / материалов нет вовсе; `409` — уже выполняется.

### `POST /api/stop/{session_id}`
Кооперативная остановка: флаг проверяется узлами графа **между** LLM-вызовами; текущая
реплика доигрывается. Сессия → `stopped`, отчёт не генерируется, материалы правятся,
повторный `run` разрешён.
→ `{"session_id": "…", "status": "stopping"}`. Ошибки: `404`/`409` (не running).

### `GET /api/session/{session_id}`
Прогресс: `{"session_id", "status": "created|ready|running|done|stopped|error",
"target_side", "created_at", "jurisdiction", "max_rounds", "events_received", "error"}`.

### `GET /api/report/{session_id}?format=md|json`
Итоговый отчёт; доступен **только для `done`** (для `stopped`/`error` — `409`).

- `format=md` (по умолчанию) — `text/markdown`: параметры, выжимка дела, прения по
  раундам, вердикт, подтверждённые нормы, таблица «Потребление ресурсов», блок рекомендаций.
- `format=json` — структура ниже. Типы фронтенда — `web/lib/api.ts` (держать синхронно):

```jsonc
{
  "session_id": "…", "status": "done", "target_side": "claimant",
  "created_at": "2026-09-06T12:00:00", "jurisdiction": "…", "max_rounds": 2,
  "events_received": 1234, "error": null,
  "stopped": false,
  "cost_summary": {
    "calls": [
      { "role": "claimant_lawyer", "model": "~z-ai/glm-flash-latest",
        "usage": { "input_tokens": 1460, "output_tokens": 835, "cached_tokens": 0,
                   "reasoning_tokens": 0, "total_tokens": 2295 },
        "cost_usd": 0.0198 }
    ],
    "totals": { "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                "reasoning_tokens": 0, "total_tokens": 28867 },
    "total_cost_usd": 13.2166,
    "cost_available": true,
    "currency": "RUB"
  },
  "params": {
    "jurisdiction": "…",
    "models": { "claimant_lawyer": "…", "defendant_lawyer": "…", "judge": "…" },
    "max_rounds": 2, "rounds_played": 1, "finished_by_judge": false
  },
  "history": [ { "speaker": "claimant_lawyer", "speaker_title": "Юрист заявителя",
                 "round": 1, "text": "…" } ],
  "verdict": "…",
  "verified_norms": [ { "title": "…", "number": "266-ФЗ", "date": "2026-07-26T00:00:00",
                        "url": "https://publication.pravo.gov.ru/Document/View/…" } ],
  "legal_warning": null,
  "recommendations": { "target_side": "claimant", "side_title": "заявителя",
                       "prospects": "высокие", "text": "markdown" },
  "report_file": "D:\\…\\output\\verdict_….md"
}
```

`cost_usd`/`total_cost_usd` = `null`, если роутер не отдал прайсы для модели
(тогда `cost_available: false` — показываются только токены). `currency` — валюта
отображения прайсов: `RUB` (₽) для routerai, иначе `USD` ($); числовые значения
`*_cost_usd` — в единицах прайсов роутера без конвертации. В `calls` входят и
вызовы веб-поиска с ролью `legal_research`.

### `GET /api/report/{session_id}/download`
Markdown-файл отчёта как вложение. `409`, если отчёта нет.

## Правовой research layer (этап 3)

### `GET /api/session/{session_id}/evidence`
Evidence Pack сессии: карточки источников (LAW/CASE/DOC) со статусами верификации,
выдержками, URL первоисточника; статусы провайдеров; `case_law_coverage`
(searched/not_searched/coverage/warning); warnings. Читается из
`sessions/<id>/evidence_pack.json`.
Ошибки: `404` — сессии нет; `409` — pack ещё не собран (запустите симуляцию).

Формат (типы фронтенда — `web/lib/api.ts`):

```jsonc
{
  "case_id": "…", "jurisdiction": "…", "generated_at": "…",
  "legal_issues": ["…"],
  "sources": [{
    "id": "LAW-001", "source_type": "statute",
    "title": "…", "authority": "pravo.gov.ru", "citation": "…",
    "excerpt": "точная выдержка или пусто",
    "official_url": "https://publication.pravo.gov.ru/document/…",
    "effective_date": "2026-07-26", "decision_date": null,
    "case_number": null, "court": null,
    "verified": false,
    "verification_status": "partially_verified",
    "provider": "pravo_gov", "retrieved_at": "…",
    "relevance_score": 0.0, "supports_issues": [],
    "warning": "…", "authority_level": null
  }],
  "provider_statuses": [{
    "provider": "pravo_gov", "status": "healthy", "transport": "direct_api",
    "checked_at": "…", "capabilities": ["statutes"], "message": "…"
  }],
  "warnings": ["…"],
  "case_law_coverage": {
    "searched_sources": ["supreme_court_official"],
    "not_searched_sources": ["kad_arbitr", "sudact"],
    "coverage": "official_only",   // official_only | limited | unavailable
    "warning": "…"
  }
}
```

### `GET /api/legal/health`
Healthcheck всех правовых провайдеров (без запуска симуляции):

```jsonc
{
  "checked_at": "2026-09-06T19:13:37",
  "providers": [
    { "provider": "supreme_court_official", "status": "healthy",
      "transport": "direct_api", "checked_at": "…",
      "capabilities": ["case_law"], "message": "…" },
    { "provider": "pravo_gov", "status": "healthy", "…": "…" },
    { "provider": "sonar_web_search", "status": "healthy",
      "transport": "web_search", "checked_at": "…",
      "capabilities": ["statutes", "case_law"],
      "message": "sonar доступен (…); результаты требуют сверки" }
  ]
}
```

Набор провайдеров зависит от `legal_research.web_search` в config.yaml: при
`false` провайдер `sonar_web_search` не создаётся.

### `POST /api/legal/diagnostics`
Полная диагностика pravo-mcp (шаг 1 этапа 3). Тело опционально:
`{"query": "статья 309 ГК РФ"}`. → структурированный отчёт:

```jsonc
{
  "target": "pravo-mcp (MCP pravo.gov.ru)",
  "started_at": "…", "duration_s": 3.4, "ok": true,
  "checks": [
    { "check": "package", "status": "ok", "details": {…},
      "error_type": null, "error_chain": [], "suggested_action": null },
    // package, config, transport, mcp_session, search_probe, document_completeness
  ]
}
```
`ok=false`, если хотя бы один check в статусе `fail` (warn/skip не считаются
провалом). CLI-эквивалент: `python -m src.legal_diagnostics --json`.

## WebSocket

### `WS /ws/session/{session_id}`

- На подключении — `{"type": "status", "payload": {"status": "…", "target_side": "…"}}`;
- далее поток `DebateEvent` в JSON (таблица типов — ARCHITECTURE.md); в этапе 3
  добавлены события `legal_research_started`, `provider_status`,
  `legal_source_found`, `evidence_pack_ready` (перед прениями); при
  переквалификации дела судьёй `judge_decision` несёт `payload.requalify` +
  `payload.requalify_reason`, и блок research-событий повторяется;
- события буферизуются на сервере: **при переподключении клиент получает их с начала
  (replay)**, затем новые (опрос новых — раз в 0.2 с); несколько клиентов — независимые
  ленты;
- сервер закрывает соединение после `debate_done`/`error`; для несуществующей сессии —
  событие `error` и close.

Типовой клиентский цикл (как в `web/lib/api.ts`): слушать до закрытия, затем
`GET /api/report/…` — `200` → финальный экран (+ параллельно
`GET /api/session/{id}/evidence` для секции «Правовые источники»), `409` → сессия
`stopped`/`error` → возврат к материалам.

## Пример сквозного сценария (PowerShell)

```powershell
$body = @{ base_url='https://routerai.ru/api/v1'; api_key='';
  model_claimant_lawyer='~z-ai/glm-flash-latest';
  model_defendant_lawyer='~z-ai/glm-flash-latest';
  model_judge='~z-ai/glm-flash-latest';
  jurisdiction='РФ, гражданское право'; max_rounds=1; target_side='claimant' } | ConvertTo-Json
$s = Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/setup -ContentType 'application/json' -Body $body
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/upload/$($s.session_id)" -Form @{context='Суть спора…'}
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/run/$($s.session_id)"
# … читать события через WS-клиент или GET /api/session/{id} …
Invoke-RestMethod "http://127.0.0.1:8000/api/report/$($s.session_id)?format=json"
```

