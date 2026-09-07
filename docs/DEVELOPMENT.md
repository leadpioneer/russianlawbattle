# Руководство разработчика

Как поднять dev-окружение, проверять изменения, где что лежит и как расширять.
Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md), API — [API.md](API.md).

## Dev-окружение

```powershell
# Python 3.11+ (проект разрабатывался на 3.14), Node.js LTS
py -3.14 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # включает локальный wheel wheels/pravo_mcp-…whl

cd web; npm install; cd ..               # зависимости фронтенда
```

Запуск в dev-режиме (три процесса в разных окнах):

```powershell
.venv\Scripts\python.exe -X utf8 -m uvicorn src.api:app --port 8000 --reload   # бэкенд с автоперезагрузкой
cd web; npm run dev                                                             # фронт с HMR (:3000)
.venv\Scripts\python.exe -X utf8 -m src.main run --max-rounds 1 -v              # CLI (debug-режим)
```

Ключи: `.env` в корне (`ROUTERAI_API_KEY=…`, имя переменной — из `api_key_env` в
`config.yaml`). Шаблоны: `.env.example`, `config.yaml.example`. Секреты в git не попадают
(.gitignore): `.env`, `config.yaml`, `case_context.md`, `case_files/*`, `output/*`,
`sessions/*`, `*.log`, `web/node_modules`, `web/.next`.

## Как проверять изменения

1. **Синтаксис/импорты бэкенда**: `.venv\Scripts\python.exe -X utf8 -c "import src.api, src.graph, src.main"`.
2. **Фронтенд**: `cd web; npm run build` — это gate: TypeScript + ESLint + генерация статики.
3. **Живой e2e** — конвенция `_check_*.py`-скриптов в корне (в git не попадают, создаются
   под задачу и удаляются): `setup → upload → run → читать WS → проверки отчёта` против
   живого роутера с `max_rounds=1` (~2–4 мин). Образцы проверок:
   полный поток событий (`agent_start×5, judge_decision, recommendations_done,
   debate_done`), `usage` в payload `agent_end`, `cost_summary` в JSON, статус-код
   ошибочных сценариев. Запускать с `-u` (иначе stdout буферизуется при редиректе).
4. **Проверить вручную в браузере** всё, что касается UX (клики по файлам, drag-and-drop,
   печати) — headless это не покрывает.
5. **Метка версии** в футере `web/app/page.tsx` (`v0.x.y`) — поднимайте при любом изменении
   фронтенда: пользователь по ней видит, что бандл свежий (`next start` раздаёт собранное
   на момент старта; после pull окна нужно перезапускать).

## Карта файлов: правишь X — проверь Y

| Файл | Ответственность | Что проверить при изменении |
|---|---|---|
| `src/config.py` | конфиг `.env` + `config.yaml`, валидация, `project_root` | CLI `config`; `/api/defaults`; `web/` форму |
| `src/llm_client.py` | клиенты по ролям, streaming, usage, прайсы | живой прогон CLI; `cost_summary` в отчёте |
| `src/document_loader.py` | case_context + case_files, суммаризация | `/api/upload` без контекста; CLI-прогон |
| `src/agents/base.py` | роли, `Statement`, юрисдикция, блок дела | все агенты |
| `src/agents/*.py` | промпты и генерация реплик | живой прогон (тон текста/маркер судьи) |
| `src/agents/legal_context.py` | legacy-формирование запросов норм (фасад) | deprecated — не расширяется |
| `src/graph.py` | LangGraph, узел `build_evidence_pack`, linter, события, переквалификация, `DebateResult` | e2e WebSocket; CLI; отчёт; тест `test_graph_e2e` |
| `src/agents/judge.py` | промпты судьи, маркеры решений (ПРОДОЛЖАТЬ/ЗАВЕРШИТЬ/ПЕРЕКВАЛИФИКАЦИЯ) | парсер-тесты; e2e переквалификации |
| `src/legal_tools.py` | DEPRECATED-фасад pravo-mcp | не расширяется; снос после шага 6+ |
| `src/session_store.py` | сессии, статусы, stop, cost_summary, валюта | `/api/run`, `/api/stop`, перезапуск stopped |
| `src/api.py` | REST/WS, CORS, валидация, evidence/health/diagnostics | e2e-скрипт; `npm run build` (типы) |
| `src/report.py` | md-отчёт (+ разделы этапа 3, дисклеймер) | полный прогон, открыть `output/verdict_*.md` |
| `src/legal/models.py` | `LegalSource`/`EvidencePack`/`ProviderHealth`, инварианты верификации | unit-тесты; JSON-roundtrip |
| `src/legal/service.py` | оркестрация провайдеров, дедуп, ranking, coverage | research на mock-провайдерах |
| `src/legal/providers/pravo_gov.py` | реквизиты актов (официальный API) | живой прогон «статья N <акт>» |
| `src/legal/providers/supreme_court.py` | Пленум/обзоры ВС (vsrf.ru, уровень A) | живой healthcheck vsrf.ru |
| `src/legal/providers/sonar.py` | веб-поиск НПА и практики (sonar на роутере, plugins + url_citation) | живой прогон search_case_law/search_statutes |
| `src/legal/fulltext.py` | полная сверка источников (скачивание, ≥2 реквизитов, verified) | unit-тесты; живой прогон vsrf/pravo |
| `src/legal/providers/mock.py` | тестовые фикстуры (без сети) | unit-тесты сервиса |
| `src/legal/case_law.py` | user-акты (USER), coverage, disabled-расширения | классификация документов дела |
| `src/legal/converters.py` | тексты статей (Консультант + markitdown) | живой прогон «статья 309 ГК РФ» |
| `src/legal/citation_verifier.py` | linter ссылок + repair-pass | тесты верификатора; отчёт |
| `src/legal/issue_extractor.py` | вопросы дела (LLM + fallback) | прогон с LLM; fallback без |
| `src/legal/evidence_pack.py` | сборка pack + persist в сессию | `/api/session/{id}/evidence` |
| `src/legal/prompts.py` | prompt-блок Evidence Pack | живой прогон (секции/rules) |
| `src/legal/diagnostics.py` | диагностика pravo-mcp | `python -m src.legal_diagnostics` |
| `src/legal_diagnostics.py` | CLI диагностики | `--json`, `--provider mock` |
| `src/main.py` | CLI (debug) | `run --max-rounds 1`, `config` |
| `web/lib/api.ts` | REST/WS-клиент, TS-типы отчёта | типы ↔ JSON API; `npm run build` |
| `web/app/page.tsx` | весь UI-мастер | браузер: все 4 экрана |
| `install.bat` | установка/запуск пользователем | прогон `install.bat --no-start`; скобки в echo! |
| `wheels/` | локальный wheel pravo-mcp | см. рецепт обновления ниже |

## Рецепты

### Добавить пресет модели
`web/app/page.tsx` → массив `MODEL_PRESETS` (label + алиас). Проверить, что роутер отдаёт
прайс для алиаса: probe-скрипт `fetch_model_pricing(...)` + `alias in pricing`.

### Добавить агента/узел графа
1. `src/agents/<name>.py` — промпт + функция генерации (шаблон: `claimant_lawyer.py`);
   `chat()` вернёт `LlmResult` — `.text`/`.usage` доступны.
2. `src/graph.py`: константа `NODE_*`, функция узла (шаблон `recommendations_node`:
   `stopped_now()` → emit `agent_start/delta/agent_end` с `payload.usage` → return),
   `add_node` + рёбра; при необходимости — поле в `DebateState`/`DebateResult`.
3. `src/report.py` — секция в отчёте; `src/api.py` — поле в JSON; `web/app/page.tsx` — UI.
4. Живой прогон `max_rounds=1` + браузер.

### Добавить тип события
`graph.py`: константа `EVENT_*`, `emit(DebateEvent(...))` из нужного места. Клиенты:
WS-обработчик в `runDebate` (`page.tsx`), при желании — ветка в CLI. Опишите в таблице
событий ARCHITECTURE.md.

### Обновить wheel pravo-mcp
```powershell
.venv\Scripts\python.exe -m pip wheel "git+https://github.com/AlsKozlov/ru-legal.git#subdirectory=mcps/pravo" -w wheels --no-deps
```
(старый файл из `wheels/` удалить, в `requirements.txt` путь не меняется, если версия та же).

### Сменить роутер
Ничего в коде менять не нужно: `api_base_url` + `api_key_env`/ключ в `.env`. Учесть:
префикс `~` в алиасах — особенность routerai; на OpenRouter алиасы вида `vendor/model`;
`llm_params.reasoning` поддерживают не все роутеры (лишний параметр у совместимых —
ошибка 400, уберите из `config.yaml`/формы).

### Включить/выключить поиск норм
`config.yaml` → `legal_mcp: {enabled: false}`. Отключение «тихое» — предупреждений
в отчёте не будет (в отличие от недоступности сервера).

### Включить/выключить веб-поиск и сменить поисковую модель
```yaml
legal_research:
  web_search: true                       # false — sonar-провайдер не создаётся
  search_model: "perplexity/sonar-pro-search"  # алиас поисковой модели роутера
```
Приоритет алиаса: `search_model` из конфига → env `SONAR_MODEL` → дефолт
(`perplexity/sonar-pro-search`). Модель обязана поддерживать веб-поиск
(Perplexity Sonar и аналоги) — обычная chat-модель источники не найдёт.
Провайдер фильтрует ссылки белым списком официальных доменов
(`_ALLOWED_URL_HOSTS` в `sonar.py` — расширять при добавлении источников).

### Добавить маркер решения судьи
`src/agents/judge.py`: вариант в `_JUDGE_MARKER_INSTRUCTION` + ветка в regex
`_MARKER_RE` + поля `JudgeDecision` + обработка в `parse_judge_decision`. Клиенты:
`should_continue()` в `graph.py` (переходы + mapping `add_conditional_edges`!),
событие `judge_decision`, UI в `page.tsx`. Тесты: `test_requalification.py`.

### Добавить провайдера правового исследования
1. `src/legal/providers/<name>.py` — класс с `name`, `healthcheck()`,
   `search_statutes()`, `search_case_law()`, `get_document()` (шаблон: `sonar.py` —
   ленивые креденшелы из конфига, честные `verification_status`, `[]` при ошибке).
2. Зарегистрировать в `_PROVIDER_REGISTRY` и `default_provider_configs()`
   (`service.py`).
3. Unit-тесты на моках; живой прогон; описание в ARCHITECTURE.md.

### Выделить отдельную модель аналитику (не судье)
Сейчас аналитик переиспользует роль `judge`. План: добавить роль в `ROLE_TO_MODEL_KEY`
(`config.py`) + поле `Config.model_*` + пресет в форме → `ADVISOR_ROLE = "advisor"` в
`advisor.py` (`chat(ADVISOR_ROLE, …)`) → ключ в `_ALLOWED_KEYS` и в `SetupRequest`.

## Соглашения проекта

- **Коммиты**: `git -c user.name='dev' -c user.email='dev@local' commit` (локальные
  настройки репо не заданы); осмысленные сообщения в формате `Step X.Y: …` / `Fix …` /
  `Feature: …` (история — см. CHANGELOG).
- **Python**: всегда `.venv\Scripts\python.exe -X utf8 …` (UTF-8 stdout на Windows);
  запуск из корня проекта (`-m src.…`).
- **Кодировка в bat**: только `echo` без скобок `()` внутри if/else-блоков — скобка
  закрывает блок при парсинге (грабли, на которые наступали дважды).
- **PowerShell**: вложенные кавычки ломают inline-скрипты — многострочный python-код
  писать во временный файл (`Set-Content -Encoding UTF8` + запуск), редиректы
  `2>err.log`; зависший `pause` в bat держит файл лога открытым.
- **Планирование больших LLM-прогонов**: `Start-Process` с redirect в логи + poll
  `Get-Content -Tail`; e2e-скрипты с `-u`.
- **Секреты**: ключ API только в `.env` (или в памяти сессии через форму); в git —
  только `.example`-шаблоны; перед пушем — `git grep --cached` по маскам ключей.
- **Документация**: правки контрактов (события, JSON отчёта) синхронизировать в трёх
  местах: `graph.py`/`api.py` ↔ `web/lib/api.ts` ↔ `docs/`.

## Отладка: типичные проблемы разработки

| Симптом | Причина | Что делать |
|---|---|---|
| Фронт ведёт себя «по-старому» | stale-бандл `next start` | пересобрать (`npm run build`) и перезапустить окно фронта; сверить v-метку в футере |
| `Failed to fetch` в браузере | бэкенд не запущен | индикатор в шапке красный; поднять uvicorn |
| `KeyError: '__end__'` в графе | `END` не в mapping условного ребра | добавить `END: END` в `add_conditional_edges` |
| `str() got an unexpected keyword` | dataclass поверх `str` | `LlmResult.__new__` явный (см. llm_client) |
| CLI падает `Path(None)` | `load_config(None)` | в `run_debate` вызов условный: `load_config(config_path) if config_path else load_config()` |
| pip падает на `pravo-mcp` при clone | сетевые сбои GitHub | ставить из `wheels/` (уже так в requirements) |
| usage нули в отчёте | роутер не отдал usage в стриме | токены «нет данных»; деньги недоступны — это graceful-путь |

