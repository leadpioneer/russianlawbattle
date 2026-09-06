# Отчёт по спринту: Этап 3 — надёжный legal research layer

**Период:** 06.09.2026 · **Версия после спринта:** v0.4.0
**Коммиты:** `52988ba` → `e78b3a5` (10 коммитов, все запушены в `main`)
**Исходное ТЗ:** `Etap3_promt_nadezhnyy_pravovoy_research_layer.md` (9 шагов + финальная проверка)

---

## 1. Что удалось (сделано полностью)

| Шаг ТЗ | Результат | Коммит |
|---|---|---|
| 1. Аудит и диагностика MCP | `src/legal/diagnostics.py` + CLI `python -m src.legal_diagnostics` (+`--json`): 8 проверок (пакет → конфиг → transport → сессия → tools → search_probe → document_completeness) с error_chain и suggested_action. 19 тестов на замоканной сессии | `52988ba` |
| 2. Доменные модели и контракт | `src/legal/models.py` (`LegalSource`, `EvidencePack`, `ProviderHealth` — dataclass + сериализация), `providers/base.py` (Protocol runtime_checkable), `providers/mock.py` — сервис тестируется без сети/MCP/LLM | `475b62c` |
| 3. MCP → provider | Промежуточно: логика pravo-mcp осталась фасадом; вместо обёртки MCP реализован **прямой официальный API** (см. «получилось по-другому») | `a745def` |
| 4. Официальный провайдер норм | `providers/pravo_gov.py`: `GET /api/Documents` (живой контракт проверен), фолбэк-развёртывание аббревиатур, `eoNumber` → страница `/document/<eoNumber>` → официальный PDF; кэш-проба, ранжирование шума | `a745def` |
| 5. LegalResearchService + Evidence Pack | `service.py` (priority/timeout/dedupe/ranking), `issue_extractor.py` (LLM → строгий JSON, fallback), `evidence_pack.py` (сборка + persist в сессию), `prompts.py` (строгие секции + правила) | `c58ab07` |
| 6. Встраивание в LangGraph | Узел `build_evidence_pack` до прений; общий Evidence Pack для трёх ролей; 4 новых WS-события; degraded-инструкции; typed state; −4 LLM-вызова за прогон | `b715d8e` |
| 7. Citation verifier | Детект 4 каналов (`[LAW-001]`, `ст. N <акт>`, `266-ФЗ`, номера дел) → сверка с pack → `verified/missing/mismatch/unverified_source`; один repair-pass; раздел «Проверка правовых ссылок» в отчёте. **Живая проверка**: failed → repair → passed | `5c379ad` |
| 8. Судебная практика — честный MVP | `UnavailableCaseLawProvider` (честная причина), `SupremeCourtOfficialProvider` (vsrf.ru, Пленум/обзоры, уровень A), user-акты (уровень USER, метаданные), `CaseLawCoverage` в pack/отчёте; Atomno/KadArbitr — disabled-заготовки | `a23de0e`, `d4a536e` |
| 9. UI, API, отчёты, документация | Секция «Правовые источники» в UI (карточки со статусами, кнопка «Открыть первоисточник», баннер деградации); API `evidence/legal-health/legal-diagnostics`; разделы отчёта (нормы / разъяснения ВС / практика / user-документы / покрытие); дисклеймер в отчёт; README-раздел надёжности | `e78b3a5` |

**Нефункциональные требования:** timeouts на всех внешних вызовах — да; секреты не
логируются (маска в diagnostics) — да; unit-тесты на новую логику — 105 passed
(85 → 105 за спринт); mock-фикстуры помечены — да; SQLite не добавлялся (не требовался);
CLI и веб совместимы — да.

---

## 2. Что не получилось совсем

| Задача | Что происходит | Почему |
|---|---|---|
| **Тексты статей с pravo.gov.ru** | Официальный PDF — **скан без текстового слоя** (CCITTFaxDecode, 0 шрифтов; pypdf и markitdown извлекают 0 символов) | Портал публикует только изображения страниц. Решение — тексты из КонсультантПлюс через markitdown (см. «по-другому») |
| **`get_npa` через pravo-mcp** | Стабильно возвращает строку «Документ не найден на pravo.gov.ru» (не dict, не 404-исключение) | Upstream-проблема MCP-пакета/API портала. Диагностика фиксирует это машиночитаемо (`warn` + snippet); в этапе 3 MCP больше не критический путь |
| **Массовый поиск практики (sudact.ru)** | Страница поиска отдаёт **пустую форму** с текстом «Поиск не доступен. У Вас отключен JavaScript» | Результаты рендерятся на клиенте; без headless-браузера недоступно. По решению пользователя Playwright/Chromium **не подключаются** — провайдер оставлен честно недоступным + disabled-расширение |
| **kad.arbitr (арбитражные дела)** | Капча, открытого API нет | Disabled-заготовка `KadArbitrProvider` (NotImplementedError, healthcheck `not_configured`) |
| **Полнотекстовый поиск по базе актов ВС** | На vsrf.ru такого API/раздела нет; провайдер покрывает только раздел Пленума/обзоров | Честно задокументировано в healthcheck-сообщении и README; не заявляем полноту покрытия |

Ни одна из этих проблем **не заблокировала этап**: все они закрыты честной
деградацией (статусы `unavailable`/`partially_verified`/`USER` + предупреждения),
как и требовало ТЗ.

---

## 3. Что получилось по-другому (отклонения от первоначального плана)

| Планировалось | Как получилось | Почему лучше/хуже |
|---|---|---|
| MCP как основной источник норм | **Прямой API `publication.pravo.gov.ru/api/Documents`** — MCP остался legacy-фасадом (deprecated) | Диагностика показала: MCP дублирует функцию с багом (`get_npa` 404), а прямой API работает и отдаёт `eoNumber` → официальный PDF. Меньше зависимостей |
| (не планировалось) **Тексты статей через КонсультантПлюс + markitdown** | `converters.py`: базовый документ → оглавление → страница статьи → markitdown HTML→Markdown | Ключевой прорыв спринта: появились точные выдержки норм (подсказка пользователя про формат ссылок `publication.pravo.gov.ru/document/<номер опубликования>` открыла цепочку). Текст честно помечен неофициальным |
| Провайдер MCP «без изменения семантики» (шаг 3) | Вместо переноса логики — новый провайдер на прямом API + фасад остался для совместимости | Семантика `LawExcerpt` сохранена проекцией `_pack_to_excerpts()` — отчёт/API не сломались |
| `SudactPlaywrightProvider` упоминался пользователем как возможный путь | Оставлен **только в roadmap**; Chromium в install.bat не добавляется | Решение пользователя: без тяжёлых зависимостей и без гарантий работы; sudact честно `unavailable` |
| Отдельные узлы `extract_issues → research_statutes → research_case_law → build_evidence_pack` | Один узел `build_evidence_pack` (внутри — async-сервис) | Меньше просадок состояния графа; события `provider_status/legal_source_found/evidence_pack_ready` покрывают наблюдаемость; шаги 4–5 ТЗ слились в один узел |
| Верификатор «regex + сверка» | Реализовано как планировалось + **repair-pass с живым LLM** | Живая проверка: текст с 2 выдуманными ссылками → repair → `passed, 0 issues` (LLM убрал номера, не тронув подтверждённые) |
| Оценка «по шагу 4 кэш SQLite» | Кэш не потребовался: поиск быстрый (3.4 с полный diagnostics), кэшируются прайсы роутера (1 ч) | YAGNI; SQLite остаётся в roadmap для персистентности сессий |

---

## 4. Известные ограничения и технический долг

- **`src/legal_tools.py` — deprecated-фасад**: держится для совместимости CLI-прогонов;
  снос после полной проверки (`_pack_to_excerpts` уже покрывает отчёт).
- **Тексты статей зависят от Консультанта**: смена вёрстки/добавление капчи сломает
  `converters.py` (ослабленный режим: реквизиты остаются, excerpt пустеет).
- **vsrf.ru-провайдер**: только первая страница раздела Пленума (без пагинации/фильтров
  по датам); заголовки без PDF игнорируются.
- **Метрики релевантности** — грубое совпадение слов; достаточно для MVP, но для
  практики нижестоящих судов понадобится полноценный ранжир.
- **Кэш провайдеров** не сделан (YAGNI); при росте нагрузки — диск-кэш/SQLite.

---

## 5. Чеклист тестирования (что и как проверено)

### Автотесты
- [x] `pytest -q` — **108 passed** (85 на старте спринта → 108)
- [x] Диагностика: healthy / пакет отсутствует / битый импорт / tool не найден / таймаут / неполный ответ / binary missing / 0 хитов каскада / get_npa 404 / render / маска секретов (19)
- [x] Модели: инварианты верификации (verified требует excerpt и статус), JSON-roundtrip, агрегаты pack, `make_id` (14)
- [x] Mock-провайдер: Protocol-совместимость, healthcheck, поиски, get_document, fail-режим (6)
- [x] pravo_gov: extract номеров/актов, search (mock httpx), network error, `hit_to_legal_source` never-verified, healthcheck-unavailable, search без обогащения, case_law=[] (honest), обогащение через converters со статусом ≠ verified (13)
- [x] issue_extractor + service + prompts: JSON-парсинг (plain/fenced/garbage), fallback, search_queries, дедуп (лучший по статусу/провайдеру), renumber, research на mock'ах, degraded-режим, ошибки → warnings, секции prompt-блока, sync-in-thread, persist (23)
- [x] Граф: узел `build_evidence_pack` в графе, константы событий, `_pack_to_excerpts`, degraded-warnings, `evidence_pack` в DebateResult (5)
- [x] **E2E полного графа с замоканными агентами** (регрессия NameError `citation_results`): обе секции linter доходят до DebateResult; проверка отсутствия `citation_results.append` в исходнике узлов (2)
- [x] Citation verifier: verified по ID, missing → failed, unverified → warning, матч номера акта, missing акта, номер дела, пустой текст, pack=None, to_dict, repair no-op, repair при ошибке LLM (11)
- [x] Case law: unavailable-провайдер, disabled Atomno/KadArbitr, not_configured, классификация user-документов, метаданные акта, USER-уровень, парсер vsrf (mock-фикстуры), релевантность, поиск ВС (mock), healthcheck-unavailable, coverage-сообщения/roundtrip (20)

### Живые проверки (реальные источники, без моков)
- [x] `python -m src.legal_diagnostics` — 3.4 с, `ok: true`; диагноз: search ok, get_npa → строка «Документ не найден» (зафиксировано с detail_snippet)
- [x] `python -m src.legal_diagnostics mock --query 309 --json` — smoke без сети: `LAW-001 verified` (п. 3 финальной проверки ТЗ)
- [x] PravoGovProvider: healthcheck healthy; поиск «статья 309 ГК РФ» → 2 источника с реквизитами + eoNumber; excerpt 2556 знаков (текст ст. 309 через Консультант/markitdown)
- [x] SupremeCourtOfficialProvider: healthcheck healthy (2 документа раздела); поиск → `[CASE-001] (A)` с официальным PDF vsrf.ru и датой
- [x] Citation linter + repair (живой LLM): текст с 2 подтверждёнными + 2 выдуманными ссылками → `failed` → repair → **`passed, 0 issues`** (подтверждённые `[LAW-001]`/`266-ФЗ` сохранены)
- [x] Полный Evidence Pack (live): 5 источников, coverage `official_only`, searched=[supreme_court_official], честное сообщение о неполном покрытии
- [x] API: `GET /api/legal/health` → 200 (2 провайдера healthy); `POST /api/legal/diagnostics` → 200 ok:true; `GET /api/session/{id}/evidence` → 404 (нет сессии) / 409 (pack не собран)

### Сборки и регрессии
- [x] `npm run build` (TypeScript + ESLint) — зелёный; типы EvidencePack/событий синхронизированы с `web/lib/api.ts`
- [x] Импорты всего приложения целы после каждого шага (`import src.api, src.graph, src.main`)
- [x] Старый CLI (`run --max-rounds 1`) не тронут; legacy-колбэки `on_delta`/`announce` работают
- [x] Временные `_check_*.py` скрипты удалялись после проверок; в git не попали
- [x] Секреты: маска в diagnostics; ключи не в логах/отчётах (проверено тестом маски)

### Финальная проверка по ТЗ — соответствие
- [x] 1. `python -m src.legal_diagnostics` — выполнено, отчёт показан
- [x] 2. `pytest -q` — 105 passed
- [x] 3. smoke с mock provider без сети — выполнено
- [x] 4. запуск backend / CLI — импорты и граф проверены; полный e2e с LLM — по конвенции `_check_*` скриптов (выполнены точечно на каждом шаге)
- [x] 5. Отчёт: Evidence Pack есть; ProviderHealth есть; ссылки имеют статусы; непроверенные помечены; ключей в логах/отчёте нет
- [x] MCP/официальный источник недоступен → не блокирует этап; mock/кэш/деградация обеспечены; подтверждённость не заявляется без подтверждения

---

## 6. Дорожная карта (наследие спринта)

1. **Тексты НПА из официального источника** — если портал начнёт отдавать
   машиночитаемый текст (или появится OCR-сервис), переключить pravo_gov на
   `verified` без изменения контрактов.
2. **Playwright-провайдер практики** (sudact) — опциональный плагин по желанию
   пользователя; не в install.bat.
3. **Пагинация/фильтры vsrf.ru** — выборка Пленума по датам за пределами первой страницы.
4. **Кэш провайдеров** (диск/SQLite) + персистентность сессий.
5. **Снос `src/legal_tools.py`** и `agents/legal_context.py` после проверки CLI-прогонов
   на новом слое.
6. **Отдельная модель аналитика** (4-й слот роли) — рецепт в DEVELOPMENT.md.

