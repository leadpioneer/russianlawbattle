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
│    sessions/<id>/{case_context.md, case_files/, output/,    │
│                  evidence_pack.json}                        │
└──────────────┬──────────────────────────────────────────────┘
               │ run_debate(cfg=…, target_side=…, sink=…, should_stop=…)
┌──────────────▼──────────────────────────────────────────────┐
│  src/graph.py  LangGraph StateMachine                       │
│    init → build_evidence_pack → claimant_turn               │
│           → defendant_turn → judge_review                   │
│    judge_review ─?(ПРОДОЛЖАТЬ и round < max)→ claimant_turn │
│    judge_review ─?(ПЕРЕКВАЛИФИКАЦИЯ, ≤ 2 раз)→ build_evidence_pack │
│    judge_review ─?(иначе)─────────────────→ final_verdict   │
│    final_verdict ─(linter)────────────────→ recommendations │
│    recommendations ─(linter)──────────────→ END             │
│    любой узел ─?(should_stop)─────────────→ END (stopped)   │
└───────┬──────────────────────────────┬──────────────────────┘
        │ chat(role, …)                │ LegalResearchService
┌───────▼────────────────┐   ┌─────────▼──────────────────────────────────┐
│ src/llm_client.py      │   │ src/legal/  (правовой research layer,      │
│ openai SDK (streaming) │   │   этап 3)                                  │
│ usage из стрима        │   │  service.py — оркестрация провайдеров      │
│ прайсы из GET /models  │   │  providers/pravo_gov.py — реквизиты актов  │
│ log_external_usage()   │   │  providers/supreme_court.py — Пленум ВС    │
│ citation_verifier.py   │   │  providers/sonar.py — веб-поиск (роутер)   │
│ (linter ссылок)        │   │  converters.py — тексты статей (markitdown)│
└───────┬────────────────┘   │  case_law.py — user-акты, coverage         │
        │                    │  evidence_pack.py, prompts.py, models.py   │
        │                    └───────┬──────────┬──────────┬──────────────┘
        │                            │ HTTP     │ HTTP     │ [OI] chat
        │                    ┌───────▼────────┐ ┌─────▼─────┐ ┌──▼─────────────┐
        │                    │ publication.   │ │ www.vsrf.ru│ │ поисковая модель│
        │                    │ pravo.gov.ru   │ │ (Пленум)   │ │ (sonar) роутера│
        │                    └───────┬────────┘ └────────────┘ └────────────────┘
        │                            │ (тексты статей — markitdown)
        │                    ┌───────▼────────┐
        │                    │ КонсультантПлюс│  (неофициальный текст, помечается)
        │                    └────────────────┘
        ▼
  [OI]-совместимый роутер (routerai.ru / OpenRouter / vLLM / …)
```

Ключевой принцип: **одна и та же логика обслуживает и CLI, и веб**. CLI (`src/main.py`)
и FastAPI — два тонких адаптера над `run_debate()`: первый подписывается на события
колбэками `announce`/`on_delta` (печать в консоль), второй — через `sink` (список
`DebateEvent` в сессии, который WS-обработчик раздаёт клиентам).

## Граф прений (LangGraph)

Узлы объявлены в `build_graph()`; состояние — `DebateState` (TypedDict; для `history`,
`norms_used`, `legal_warnings`, `citation_results` редьюсер `operator.add` — узлы
возвращают частичные обновления, LangGraph их складывает).

| Узел | Делает | Возвращает |
|---|---|---|
| `init` | Загружает материалы дела (кэш по `project_root`) | `cfg, materials, round_number=0` |
| `build_evidence_pack` | **Этап 3**: вопросы дела → провайдеры → Evidence Pack + события; при переквалификации — повторный сбор | `legal_issues, evidence_pack, evidence_block, research_runs` |
| `claimant_turn` | Реплика юриста заявителя (с общим Evidence Pack) | `history+, round_number, norms_used+, legal_warnings+` |
| `defendant_turn` | Реплика юриста ответчика (с общим Evidence Pack) | `history+, norms_used+, legal_warnings+` |
| `judge_review` | Оценка раунда + маркер решения (в т.ч. переквалификация) | `history+, judge_decision, requalifications+` |
| `final_verdict` | Итоговое решение + linter ссылок | `verdict, norms_used+, legal_warnings+, citation_results+` |
| `recommendations` | Рекомендации для target_side + linter | `recommendations, legal_warnings+, citation_results+` |

Evidence Pack собирается **до прений** (и повторно при переквалификации — см. ниже)
и передаётся всем трём ролям (каждая интерпретирует его в интересах своей стороны).
До этапа 3 каждый узел сам формулировал запросы норм через MCP (4 лишних LLM-вызова
за прогон) — это заменено единым узлом `build_evidence_pack`.

Условный переход после судьи — `should_continue()`, приоритет проверок:
1. `state["stopped"]` → `END`;
2. `judge_decision.requalify` **и** `research_runs <= MAX_REQUALIFICATIONS` (=2) →
   `build_evidence_pack` (переквалификация дела — нормативная база собирается заново);
3. `judge_decision.continues=False` или `round_number >= max_rounds` → `final_verdict`;
4. иначе → `claimant_turn`.
**Важно**: все цели перехода (включая `END` и `build_evidence_pack`) должны
присутствовать в mapping `add_conditional_edges`, иначе LangGraph бросает
`KeyError` (уже ловили дважды).

### Переквалификация дела

Судья может объявить маркером `=== РЕШЕНИЕ СУДЬИ: ПЕРЕКВАЛИФИКАЦИЯ: <новый характер
спора> ===`, если в прениях выяснилось, что характер спора изменился (пример из
промпта: товар использовался для извлечения коммерческой прибыли, а не личных нужд →
ЗоЗПП неприменим). Граф возвращается в `build_evidence_pack`: запрос расширяется
причиной переквалификации и последними репликами, Evidence Pack пересобирается
(ключи `evidence_pack`/`evidence_block` перезаписываются), прения продолжаются
в рамках `max_rounds`. Причины накапливаются в `requalifications` (reducer
`operator.add`) и попадают в `DebateResult.requalifications`/`research_runs` →
строку в отчёте. Лимит `MAX_REQUALIFICATIONS = 2` защищает от цикла
research → прения → research.

### Решение судьи — строковый маркер

Вместо structured output / function calling судья в конце ответа ставит строку
`=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: …) ===`, `=== … ЗАВЕРШИТЬ ===` или
`=== … ПЕРЕКВАЛИФИКАЦИЯ: <описание> ===`, которую парсит `parse_judge_decision()`
(regex; fallback: маркера нет → «продолжать, вопрос обеим»). Почему так: cheap-модели
(GLM, Qwen) надёжно копируют текстовый шаблон и ненадёжно соблюдают JSON-схемы
в streaming-режиме; маркер дёшев и детерминирован.

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
| `judge_decision` | Маркер разобран | continues, addressee, **payload.requalify, payload.requalify_reason** | WS: плашка ПРОДОЛЖАТЬ/ЗАВЕРШИТЬ/ПЕРЕКВАЛИФИКАЦИЯ |
| `verdict_done` | Вердикт готов | payload: length, usage | служебное |
| `recommendations_done` | Рекомендации готовы | payload: target_side, prospects | служебное |
| `debate_done` | Граф дошёл до конца | payload: rounds_played, finished_by_judge, statements, verdict_length, recommendations_prospects, **stopped**, **usage_log[]** | WS: close; CLI: итог |
| `error` | Исключение в потоке сессии | payload: message | WS: красная плашка |
| `legal_research_started` | Узел `build_evidence_pack` начал работу | — | WS: индикатор «ищем право» |
| `provider_status` | Healthcheck провайдера | payload: provider, status, message | WS: статус источников |
| `legal_source_found` | Найден источник | payload: source_id, verification_status, citation, source_type | WS: счётчик найденного |
| `evidence_pack_ready` | Pack собран | payload: verified_count, partial_count, warning_count, total_sources | WS: сводка перед прениями |

Порядок для полного прогона `max_rounds=1`: `legal_research_started →
provider_status×N → legal_source_found×N → evidence_pack_ready →
agent_start(claimant) → delta×N → agent_end(claimant) → …(defendant)… →
judge_decision → …(вердикт)… → verdict_done → …(аналитик)… →
recommendations_done → debate_done`.
При переквалификации после `judge_decision` блок `legal_research_started → … →
evidence_pack_ready` повторяется, затем прения продолжаются; UI показывает баннер
«Идёт подготовка законодательной базы» при каждом проходе research.

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
Валюта отображения: `currency_for_base_url()` — подстрока `routerai` в адресе
роутера → RUB (символ ₽), иначе USD ($); поле `currency` в `cost_summary`
(и символ в UI, и в таблице отчёта). Числовые поля `*_cost_usd` остались
в прежних единицах прайсов роутера — конвертация не выполняется.

Веб-поиск (`sonar`) логирует потребление через `log_external_usage()` с ролью
`legal_research` — эти вызовы видны в таблице «Потребление ресурсов» и входят
в `cost_summary`, хотя идут мимо ролевых `chat()`.

`chat()` возвращает `LlmResult` — **наследник `str`** с полем `usage`: все места, работающие
с текстом реплик (`.strip()`, парсинг, конкатенация), не изменились. `dataclass` поверх
`str` невозможен (конфликт с `str.__new__`), поэтому `__new__` явный.

Известный дефект источника: routerai отдаёт часть `id` без имени модели (19 записей
`anthropic/…`/`~anthropic/…` с одинаковыми ключами и разными ценами) — в словаре прайсов
они схлопываются, цена для этих алиасов «примерная». Остальные проверенные модели (GLM,
Qwen, DeepSeek, Grok, Kimi, GPT-6 Astra) резолвятся строго. Если прайсов нет
(`cost_available=false`) — показываются только токены, это не ошибка.

## Правовой research layer (этап 3)

Пакет `src/legal/` — сменяемый слой провайдеров с честной моделью верификации.
Ключевое правило: **LLM не выдаёт юридическую ссылку как подтверждённую, если она
не пришла из проверяемого источника и не присутствует в Evidence Pack.**

### Доменные модели (`models.py`)

`LegalSource` (ID `LAW-001`/`CASE-001`/`DOC-001`, source_type, citation, excerpt,
verification_status, provider, authority_level), `ProviderHealth`, `EvidencePack`
(с `CaseLawCoverage`). Инвариант в `__post_init__`: `verified=True` допустим только
при `verification_status='verified'` И непустом `excerpt` — типы не позволяют выдать
непроверенный источник за подтверждённый.

Статусы: `verified` (реквизиты + точная выдержка), `partially_verified` (реквизиты
подтверждены, текст неофициальный/недоступен), `unverified`, `unavailable`,
`contradicted`. Уровни авторитетности практики: A (КС/Пленум ВС/обзор ВС),
B (ВС/кассация), C (апелляция), D (первая инстанция), USER (загружен пользователем).

### Провайдеры (`providers/`)

Контракт `LegalProvider` (Protocol, runtime_checkable): `healthcheck`,
`search_statutes`, `get_document`, `search_case_law`. Невозможность поиска — это
статус/пустой список, **никогда** не выдуманные данные.

- **`pravo_gov.py`** — официальный API публикации: `GET /api/Documents?name=…`
  (подстрочный поиск по названию; PageSize кратен 10; фолбэк-развёртывание
  аббревиатур «ГК РФ» → «Гражданский кодекс») → реквизиты + `eoNumber` → страница
  `pravo.gov.ru/document/<eoNumber>` → официальный PDF. Нормализация всегда
  `partially_verified`: текст статьи портал отдаёт только **сканом** (PDF без
  текстового слоя, CCITTFaxDecode — pypdf/markitdown извлекают 0 символов).
- **`supreme_court.py`** — `vsrf.ru/plenum.php`: карточки `<article>` с заголовками,
  датами и PDF (`/upload/iblock/…`). Пленум/обзоры → уровень A, `verified`
  (публикация на официальном домене подтверждает реквизиты). Полнотекстового
  поиска по базе актов ВС нет — только раздел Пленума; новостные карточки
  (трансляции, пресс-релизы) отфильтровываются, пустой запрос → `[]`.
- **`sonar.py`** — `SonarWebSearchProvider`: веб-поиск через плагин `web`
  роутера ([OI]-совместимый вызов с `plugins: [{id: "web", max_results: 5,
  include_domains: [официальные домены]}]`; алиас — `legal_research.search_model`
  в config.yaml или форма «Настройка»; включение — флаг `legal_research.web_search`,
  по умолчанию включён). Ищет НПА и правоприменительную практику РФ. Парсинг
  результата: сначала структурированные аннотации `url_citation` (документация
  routerai), затем текстовые форматы; домены фильтруются белым списком.
  Результаты всегда `partially_verified` + warning «сверьте по официальному
  источнику»; usage пишется с ролью `legal_research`. ВАЖНО: веб-поиск
  тарифицируется роутером отдельно от токенов (см. note в отчёте).
- **`fulltext.py`** — полная сверка веб-источников: скачивание страницы по
  `official_url` → markitdown (HTML/PDF) → поиск ≥2 реквизитов источника
  (номер акта, дата — в т.ч. текстовый формат «28 июня 2012», номер статьи).
  При совпадении: `excerpt` заменяется реальной цитатой; официальные домены
  (pravo.gov.ru, vsrf.ru) → `verified`, неофициальные (sudact) → остаются
  `partially_verified` с выдержкой. Ошибка/несовпадение → источник без
  изменений + warning. Выполняется в `service.research()` для top-4 sonar-источников
  каждого типа; флаг `legal_research.fulltext_verify`.
- **Переквалификация дела** — судья может объявить маркером
  `=== РЕШЕНИЕ СУДЬИ: ПЕРЕКВАЛИФИКАЦИЯ: <новый характер спора> ===`, если в
  прениях выяснилось, что характер спора изменился (например, товар
  использовался для коммерческой прибыли → ЗоЗПП неприменим). Граф
  возвращается в узел `build_evidence_pack` и собирает Evidence Pack заново
  с запросом, расширенным причиной переквалификации (лимит —
  `MAX_REQUALIFICATIONS = 2`); прения продолжаются в рамках `max_rounds`.
- **`mock.py`** — детерминированные фикстуры (помечены «фикстура») для тестов и
  offline-прогонов.
- **`case_law.py`** — `UnavailableCaseLawProvider` (честная заглушка массовых
  источников: sudact.ru требует JavaScript, kad.arbitr — капча) + классификация
  загруженных пользователем актов (`classify_user_document`, метаданные суда/даты/
  номера/норм, уровень USER) + `CaseLawCoverage`. Disabled-расширения
  `AtomnoCaseLawProvider`/`KadArbitrProvider` — только интерфейсы
  (NotImplementedError), healthcheck `not_configured`.

### Тексты статей (`converters.py`)

Цепочка: найти базовый документ на КонсультантПлюс → в оглавлении
(`cons_doc_LAW_<id>/<hash>/`) ссылку на статью → **markitdown** (Microsoft, MIT):
HTML → Markdown → очистка навигации. Текст честно помечается: `excerpt` заполняется,
статус остаётся `partially_verified` (источник неофициальный).

### Сервис (`service.py`)

`LegalResearchService.research()`: параллельный healthcheck → поиск в порядке
priority с таймаутом на каждого провайдера → нормы/практика раздельно →
дедупликация (лучший экземпляр: verified → pravo_gov → релевантность) →
пере-нумерация → `CaseLawCoverage`. Падение провайдера = warning в pack,
**никогда не исключение**.

### Сборка и persist (`evidence_pack.py`)

`build_evidence_pack_async` (issue_extractor → research) / sync-обёртка для узла
графа; pack сохраняется в `sessions/<id>/evidence_pack.json`
(`save/load_evidence_pack`) — для `/api/session/{id}/evidence` и отчётов.

### Citation verifier (`citation_verifier.py`)

Детект ссылок 4 каналами: `[LAW-001]`, `ст. N <Акт РФ>`, `266-ФЗ`,
`дело № А40-…/2026` → сверка с карточками pack →
`verified/missing/mismatch/unverified_source` → `passed/warning/failed`. Вердикт
и рекомендации проходят linter в графе; при проблемах — один repair-pass (LLM
убирает непроверенные номера, не выдумывая замену; при сбое LLM текст не
меняется — ошибки остаются видимыми в отчёте).

### Экстрактор вопросов (`issue_extractor.py`)

Один LLM-вызов → строгий JSON (тип спора, процедура, требования, возражения,
вопросы, факты к доказыванию). Fallback: свободный текст пользователя
(`source: user`) или разбивка на предложения (`fallback`). Результат — только
поисковые подсказки, не доказательства.

### Prompt-блок (`prompts.py`)

Секции «ПРОВЕРЕННЫЕ / ЧАСТИЧНО ПРОВЕРЕННЫЕ / НЕПРОВЕРЕННЫЕ / СУДЕБНАЯ ПРАКТИКА»
+ 6 правил цитирования (номера не выдумывать; практику не называть нормой права —
только «имеется сходная практика») + строка покрытия поиска практики.

## MCP-интеграция (pravo-mcp, legacy-фасад)

`src/legal_tools.py` (DEPRECATED-фасад, сносится после переноса всех потребителей)
поднимает `pravo_mcp.server` (локальный wheel) как stdio-подпроцесс через SDK `mcp`.
Диагностика цепочки — `python -m src.legal_diagnostics` (или
`POST /api/legal/diagnostics`).

Живой диагноз (06.09.2026): пакет/transport/session/tools — ok; `search_npa`
работает (но запросы вида «статья 309 ГК РФ» дают 0 хитов — портал ищет подстроку
по названию акта; лечится разворотом аббревиатуры); `get_npa` возвращает строку
«Документ не найден на pravo.gov.ru» — **тексты статей через MCP недоступны**,
поэтому основной источник реквизитов в этапе 3 — прямой API
`publication.pravo.gov.ru/api/Documents`, а не MCP.

## Осознанные компромиссы

| Решение | Почему |
|---|---|
| Сессии в памяти, без БД | однопользовательский локальный инструмент; перезапуск = новый сеанс; миграция на SQLite тривиальна |
| Маркер судьи вместо structured output | надёжность на cheap-моделях |
| Единый Evidence Pack до прений вместо per-turn поиска норм | 4 LLM-вызова за прогон вместо 12+; все роли видят одинаковые подтверждённые источники |
| Тексты статей из Консультанта (markitdown), не из официального PDF | официальный PDF — скан без текстового слоя (OCR — тяжёлая зависимость); текст честно помечен неофициальным |
| `partially_verified` для pravo.gov.ru-источников | реквизиты подтверждены порталом, но текст статьи извлечён извне — нельзя поднимать до verified |
| Практика ВС только из раздела Пленума | полнотекстового поиска по базе актов ВС на vsrf.ru нет; лучше честный раздел, чем ложное обещание |
| Sudact/KadArbitr — disabled-заготовки | sudact требует JS-рендеринг (Playwright/Chromium — тяжёлая зависимость и хрупкость), kad.arbitr — капча; лучше `unavailable`, чем хрупкий парсер |
| Маркер судьи вместо structured output | надёжность на cheap-моделях |
| Остановка только между репликами | аборты стрима ломают клиентскую сессию роутера |
| `LlmResult(str)` | zero-cost совместимость с 6 точками вызова |
| PDF через печать браузера | WeasyPrint/wkhtmltopdf на Windows — лишние зависимости для MVP |
| Локальный wheel pravo-mcp | пакета нет на PyPI; git-зависимость в install.bat — точка отказа |
| CORS `allow_origin_regex=".*"` | локальный инструмент; интерфейс открывают и по сетевому IP |



