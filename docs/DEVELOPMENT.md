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
| `src/agents/legal_context.py` | формулирование запросов норм | прения: блок «ПРОВЕРЕННЫЕ НОРМЫ», деградация |
| `src/graph.py` | LangGraph, события, остановка, `DebateResult` | e2e WebSocket; CLI; отчёт |
| `src/legal_tools.py` | MCP-клиент pravo.gov.ru, ранжирование | `_test_legal`-проба; предупреждение деградации |
| `src/session_store.py` | сессии, статусы, stop, cost_summary | `/api/run`, `/api/stop`, перезапуск stopped |
| `src/api.py` | REST/WS, CORS, валидация | e2e-скрипт; `npm run build` (типы) |
| `src/report.py` | md-отчёт | полный прогон, открыть `output/verdict_*.md` |
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

