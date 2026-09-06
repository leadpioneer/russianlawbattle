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
    "cost_available": true
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
(тогда `cost_available: false` — показываются только токены).

### `GET /api/report/{session_id}/download`
Markdown-файл отчёта как вложение. `409`, если отчёта нет.

## WebSocket

### `WS /ws/session/{session_id}`

- На подключении — `{"type": "status", "payload": {"status": "…", "target_side": "…"}}`;
- далее поток `DebateEvent` в JSON (таблица типов — ARCHITECTURE.md);
- события буферизуются на сервере: **при переподключении клиент получает их с начала
  (replay)**, затем новые (опрос новых — раз в 0.2 с); несколько клиентов — независимые
  ленты;
- сервер закрывает соединение после `debate_done`/`error`; для несуществующей сессии —
  событие `error` и close.

Типовой клиентский цикл (как в `web/lib/api.ts`): слушать до закрытия, затем
`GET /api/report/…` — `200` → финальный экран, `409` → сессия `stopped`/`error` →
возврат к материалам.

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

