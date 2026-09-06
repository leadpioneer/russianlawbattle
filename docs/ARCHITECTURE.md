# Архитектура

Документ для разработчиков: как устроена система, почему приняты те или иные решения,
где границы компонентов. Пользовательские инструкции — в [README](../README.md),
справочник API — в [API.md](API.md), рецепты разработки — в [DEVELOPMENT.md](DEVELOPMENT.md).

## Общая схема

```
┌────────────────────────── Браузер ──────────────────────────┐
│  web/  Next.js 16 (App Router, TS, Tailwind)                │
│    app/page.tsx   — 4-шаговый мастер (единственная страница)│
│    lib/api.ts     — клиент REST + WebSocket, типы событий   │
└──────────────┬────────────────────────────┬─────────────────┘
        REST/WS │ JSON                       │ JSON-события (DebateEvent)
┌──────────────▼────────────────────────────▼─────────────────┐
│  src/api.py  FastAPI (uvicorn, :8000)                       │
│    REST-эндпоинты + WS + CORS                               │
│  src/session_store.py                                       │
│    SessionStore (in-memory) + Session (статусы, события,    │
│    stop_event, pricing, cost_summary)                       │
│    sessions/<id>/{case_context.md, case_files/, output/}    │
└──────────────┬──────────────────────────────────────────────┘
               │ run_debate(cfg=…, target_side=…, sink=…, should_stop=…)
┌──────────────▼──────────────────────────────────────────────┐
│  src/graph.py  LangGraph StateMachine                       │
│    init → claimant_turn → defendant_turn → judge_review     │
│    judge_review ─?(ПРОДОЛЖАТЬ и round < max)→ claimant_turn │
│    judge_review ─?(иначе)─────────────────→ final_verdict   │
│    final_verdict ─────────────────────────→ recommendations │
│    любой узел ─?(should_stop)─────────────→ END (stopped)   │
└───────┬──────────────────────────────────┬──────────────────┘
        │ chat(role, …)                    │ search_law(query)
┌───────▼────────────────┐   ┌─────────────▼──────────────────┐
│ src/llm_client.py      │   │ src/legal_tools.py             │
│ openai SDK (streaming) │   │ MCP-клиент (stdio-подпроцесс   │
│ usage из стрима        │   │ pravo_mcp.server) →            │
│ прайсы из GET /models  │   │ publication.pravo.gov.ru/api   │
└───────┬────────────────┘   └────────────────────────────────┘
        ▼
  [OI]-совместимый роутер (routerai.ru / OpenRouter / vLLM / …)
```

Ключевой принцип: **одна и та же логика обслуживает и CLI, и веб**. CLI (`src/main.py`)
и FastAPI — два тонких адаптера над `run_debate()`: первый подписывается на события
колбэками `announce`/`on_delta` (печать в консоль), второй — через `sink` (список
`DebateEvent` в сессии, который WS-обработчик раздаёт клиентам).

## Граф прений (LangGraph)

Узлы объявлены в `build_graph()`; состояние — `DebateState` (TypedDict; для `history`,
`norms_used`, `legal_warnings` редьюсер `operator.add` — узлы возвращают частичные
обновления, LangGraph их складывает).

| Узел | Делает | Возвращает |
|---|---|---|
| `init` | Загружает материалы дела (кэш по `project_root`) | `cfg, materials, round_number=0` |
| `claimant_turn` | Реплика юриста заявителя (+нормы) | `history+, round_number, norms_used+, legal_warnings+` |
| `defendant_turn` | Реплика юриста ответчика (+нормы) | `history+, norms_used+, legal_warnings+` |
| `judge_review` | Оценка раунда + маркер решения | `history+, judge_decision` |
| `final_verdict` | Итоговое мотивированное решение | `verdict, norms_used+, legal_warnings+` |
| `recommendations` | Блок рекомендаций для `target_side` | `recommendations, norms_used+, legal_warnings+` |

Условный переход после судьи — `should_continue()`: `judge_decision.continues=False` или
`round_number >= max_rounds` → `final_verdict`; иначе → `claimant_turn`; если сессия
остановлена (`state["stopped"]`) → `END`. **Важно**: `END` должен присутствовать в mapping
`add_conditional_edges`, иначе LangGraph бросает `KeyError: '__end__'` (уже ловили).

### Решение судьи — строковый маркер

Вместо structured output / function calling судья в конце ответа ставит строку
`=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: …) ===` или `=== … ЗАВЕРШИТЬ ===`, которую парсит
`parse_judge_decision()` (regex; fallback: маркера нет → «продолжать, вопрос обеим»).
Почему так: cheap-модели (GLM, Qwen) надёжно копируют текстовый шаблон и ненадёжно
соблюдают JSON-схемы в streaming-режиме; маркер дёшев и детерминирован.

### Кооперативная остановка

`run_debate(should_stop=callable)` → каждый узел **перед** LLM-вызовом проверяет
`stopped_now()`; при срабатывании узел возвращает `{"stopped": True}`, условное ребро
уводит граф в END. Гранулярность — «между репликами»: текущая генерация доигрывается
(аборт стрима посреди вызова ломал бы HTTP-сессию роутера и оставлял мусор в истории).
Частичный результат сохраняется: `history` уже накоплена, `verdict=""`.

## Протокол событий DebateEvent

Единый контракт потока для CLI и WebSocket (класс в `graph.py`, TS-типы в `web/lib/api.ts` —
держать синхронно). Поля: `type` + опциональные `role, speaker_title, round, model, text,
continues, addressee, payload`; `as_dict()` отдаёт словарь без `None`-полей.

| Тип | Когда | Несёт | Потребитель |
|---|---|---|---|
| `agent_start` | Узел начинает реплику | role, speaker_title, round, **model** | WS: пузырь «печатает…»; CLI: заголовок |
| `delta` | Фрагмент стрима | role, round, text | WS: append к тексту; CLI: stdout |
| `agent_end` | Реплика готова | role, round, **text (полный)**, payload.usage | WS: замена текста, токены в счётчик |
| `judge_decision` | Маркер разобран | continues, addressee | WS: плашка ПРОДОЛЖАТЬ/ЗАВЕРШИТЬ |
| `verdict_done` | Вердикт готов | payload: length, usage | служебное |
| `recommendations_done` | Рекомендации готовы | payload: target_side, prospects | служебное |
| `debate_done` | Граф дошёл до конца | payload: rounds_played, finished_by_judge, statements, verdict_length, recommendations_prospects, **stopped**, **usage_log[]** | WS: close; CLI: итог |
| `error` | Исключение в потоке сессии | payload: message | WS: красная плашка |

Порядок для полного прогона `max_rounds=1`: `agent_start(claimant) → delta×N →
agent_end(claimant) → …(defendant)… → judge_decision → …(вердикт)… → verdict_done →
…(аналитик)… → recommendations_done → debate_done`.

## Жизненный цикл сессии

```
created ──upload──▶ ready ──run──▶ running ──┬─ успех ─────▶ done    (отчёт md + report_path)
     ▲                   ▲               ├─ should_stop ──▶ stopped (без отчёта, материалы правятся)
     └─── «← Назад» ─────┘               └─ исключение ──▶ error   (событие error в поток)
```

- **Изоляция**: каждая сессия получает каталог `sessions/<id>/`; `Config.project_root`
  указывает туда, поэтому `document_loader` (ищет `case_context.md` + `case_files/`) и
  `report` (пишет в `output/`) работают без знания о сессиях. Повторный `run` перечитывает
  материалы (`clear_case_cache()` на старте).
- **События буферизуются** в `session.events`: WS-обработчик ведёт у клиента индекс и
  досылает новое; переподключение даёт полный replay, несколько клиентов — независимые
  ленты. WS закрывается после `debate_done`/`error`.
- **Контекст дела обязателен**: если пользователь загрузил файлы без текста, `/api/run`
  сам генерирует `case_context.md` с перечнем документов; 400 — только если нет ни того,
  ни другого.
- **Потокобезопасность**: `SessionStore._lock` на словаре; `status` пишется только из
  потока симуляции; `stop_event` — штатный `threading.Event`.

## Учёт токенов и денег

Путь данных: финальные события стрима роутера (`event.usage`) → `TokenUsage.from_api()`
(поддерживает оба именования: `prompt_tokens`/`input_tokens`, details по кэшу/reasoning) →
`chat()` пишет `(роль, модель, usage)` в `_usage_log` (сброс в начале каждого `run_debate`)
→ `DebateResult.usage_log` → `build_cost_summary()` в `session_store` → `session.cost_summary`
→ JSON-отчёт и таблица в md.

Деньги: `fetch_model_pricing(base_url, api_key)` — `GET /models`, поле `pricing`
(`prompt`/`completion`/`input_cache_read` — USD **за токен**), кэш 1 час.
`estimate_cost = (input − cached)·prompt + cached·cache_read + output·completion`.

`chat()` возвращает `LlmResult` — **наследник `str`** с полем `usage`: все места, работающие
с текстом реплик (`.strip()`, парсинг, конкатенация), не изменились. `dataclass` поверх
`str` невозможен (конфликт с `str.__new__`), поэтому `__new__` явный.

Известный дефект источника: routerai отдаёт часть `id` без имени модели (19 записей
`anthropic/…`/`~anthropic/…` с одинаковыми ключами и разными ценами) — в словаре прайсов
они схлопываются, цена для этих алиасов «примерная». Остальные проверенные модели (GLM,
Qwen, DeepSeek, Grok, Kimi, GPT-6 Astra) резолвятся строго. Если прайсов нет
(`cost_available=false`) — показываются только токены, это не ошибка.

## MCP-интеграция (нормы права)

`src/legal_tools.py` поднимает `pravo_mcp.server` (пакет из локального wheel) как
**stdio-подпроцесс** и ходит в него через SDK `mcp`: `search_npa(query, limit)` →
ранжирование → (для топ-1) `get_npa(eid)`.

Перед каждой репликой `agents/legal_context.build_verified_norms()`: модель роли коротким
вызовом формулирует 1–2 запроса-**названия/номера** актов → `search_law()` → найденные
реквизиты собираются в блок «ПРОВЕРЕННЫЕ НОРМЫ ПРАВА» с правилами (ссылаться только на
них/материалы; тексты статей не загружены — без цитирования; чужие нормы —
«(требует проверки)»).

Ранжирование шума (поиск портала — подстрочный по `name`): +4 точный номер акта
(`152-ФЗ`), +3 вхождение запроса, +2 «Федерального закона / Закона РФ», −2 региональные/
«внесении изменений». Параметр `doc_type` **не передаётся** — API портала отклоняет его
(HTTP 400).

Деградация многоуровневая и никогда не роняет симуляцию: `legal_mcp.enabled=false` →
тихо без норм; не-РФ юрисдикция → без норм; MCP недоступен/пусто → `degraded=True` →
предупреждение «нормы права не подтверждены внешним источником» в md-отчёт, JSON и CLI.

Известные ограничения `pravo-mcp` (их MCP-STATUS + наша проверка): TLS портала сломан
upstream (сервер ходит по http://), `get_npa` стабильно 404 — **тексты статей недоступны**,
подтверждаются только реквизиты (номер/дата/ссылка на официальную публикацию); в реестре
публикации с ~2011 — консолидированных текстов старых кодексов нет; только федеральные
НПА. Кэш сервера 10 мин, максимум 5 параллельных запросов.

## Осознанные компромиссы

| Решение | Почему |
|---|---|
| Сессии в памяти, без БД | однопользовательский локальный инструмент; перезапуск = новый сеанс; миграция на SQLite тривиальна |
| Маркер судьи вместо structured output | надёжность на cheap-моделях |
| Явный шаг норм вместо tool calling | работает с любой моделью; tool calling в стриме хрупок на дешёвых |
| Остановка только между репликами | аборты стрима ломают клиентскую сессию роутера |
| `LlmResult(str)` | zero-cost совместимость с 6 точками вызова |
| PDF через печать браузера | WeasyPrint/wkhtmltopdf на Windows — лишние зависимости для MVP |
| Локальный wheel pravo-mcp | пакета нет на PyPI; git-зависимость в install.bat — точка отказа |
| CORS `allow_origin_regex=".*"` | локальный инструмент; интерфейс открывают и по сетевому IP |



