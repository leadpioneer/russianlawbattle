"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { DebateEvent, DefaultsData, TargetSide } from "@/lib/api";
import {
  connectSessionSocket,
  createSession,
  fetchDefaults,
  fetchReport,
  reportDownloadUrl,
  startRun,
  uploadCase,
} from "@/lib/api";

/** Ключ localStorage с настройками формы (восстанавливаются при следующем открытии). */
const SETTINGS_KEY = "court-sim-settings-v1";

interface SavedSettings {
  baseUrl: string;
  modelClaimant: string;
  modelDefendant: string;
  modelJudge: string;
  jurisdiction: string;
  maxRounds: number;
  targetSide: TargetSide;
}

function loadSavedSettings(): SavedSettings | null {
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY);
    return raw ? (JSON.parse(raw) as SavedSettings) : null;
  } catch {
    return null;
  }
}

/** Пресеты моделей — дешёвые для черновиков, сильные для финала. */
const MODEL_PRESETS: { label: string; value: string }[] = [
  { label: "GLM Flash (дешёвая)", value: "~z-ai/glm-flash-latest" },
  { label: "DeepSeek Chat", value: "deepseek/deepseek-chat" },
  { label: "GPT-4o", value: "openai/gpt-4o" },
  { label: "GPT-4o mini", value: "openai/gpt-4o-mini" },
  { label: "the model", value: "anthropic/claude-3.5-sonnet" },
];

const JURISDICTIONS = [
  "Российская Федерация, гражданское право",
  "Российская Федерация, арбитражный процесс",
  "Российская Федерация, трудовые споры",
  "Российская Федерация, защита прав потребителей (ЗоПП)",
];

type Stage = "setup" | "upload" | "live" | "final";

interface Message {
  id: number;
  speakerTitle: string;
  role: string;
  round: number;
  model: string;
  text: string;
  done: boolean;
}

const ROLE_STYLES: Record<string, string> = {
  claimant_lawyer: "border-sky-300 bg-sky-50",
  defendant_lawyer: "border-amber-300 bg-amber-50",
  judge: "border-violet-300 bg-violet-50",
};

/** Простой markdown-рендер: заголовки, списки, абзацы. */
function Markdown({ text }: { text: string }) {
  const blocks = text.trim().split(/\n{2,}/);
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      {blocks.map((block, index) => {
        const lines = block.split("\n");
        if (lines.every((line) => line.trim().startsWith("#"))) {
          return (
            <h3 key={index} className="font-semibold text-slate-900">
              {lines.map((line) => line.replace(/^#+\s*/, "")).join(" · ")}
            </h3>
          );
        }
        if (lines.every((line) => !line.trim() || /^\s*[-*•]\s+/.test(line))) {
          return (
            <ul key={index} className="list-disc space-y-1 pl-5">
              {lines.filter((line) => line.trim()).map((line, i) => (
                <li key={i}>{line.replace(/^\s*[-*•]\s+/, "")}</li>
              ))}
            </ul>
          );
        }
        return <p key={index}>{block}</p>;
      })}
    </div>
  );
}

export default function Home() {
  const [stage, setStage] = useState<Stage>("setup");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // --- форма настройки ---
  const [baseUrl, setBaseUrl] = useState("https://routerai.ru/api/v1");
  const [apiKey, setApiKey] = useState("");
  const [modelClaimant, setModelClaimant] = useState(MODEL_PRESETS[0].value);
  const [modelDefendant, setModelDefendant] = useState(MODEL_PRESETS[0].value);
  const [modelJudge, setModelJudge] = useState(MODEL_PRESETS[0].value);
  const [jurisdiction, setJurisdiction] = useState(JURISDICTIONS[3]);
  const [maxRounds, setMaxRounds] = useState(2);
  const [targetSide, setTargetSide] = useState<TargetSide>("claimant");
  // Умный режим: преднастройки из config.yaml/.env (карточка вместо формы).
  const [configDefaults, setConfigDefaults] = useState<DefaultsData | null>(null);
  const [showFullForm, setShowFullForm] = useState(false);
  const [defaultsLoaded, setDefaultsLoaded] = useState(false);
  const [clientFileError, setClientFileError] = useState<string | null>(null);

  // --- загрузка дела ---
  const [files, setFiles] = useState<File[]>([]);
  const [context, setContext] = useState("");
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // --- прямой эфир ---
  const [messages, setMessages] = useState<Message[]>([]);
  const [typing, setTyping] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(1);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- итог ---
  const [report, setReport] = useState<Awaited<ReturnType<typeof fetchReport>> | null>(null);
  const sessionIdRef = useRef<string | null>(null);

  const scrollFeed = useCallback(() => {
    requestAnimationFrame(() => {
      if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
    });
  }, []);

  // При открытии страницы: преднастройки из config.yaml/.env + сохранённая форма.
  useEffect(() => {
    let cancelled = false;
    fetchDefaults()
      .then((defaults) => {
        if (cancelled) return;
        setConfigDefaults(defaults);
        const saved = loadSavedSettings();
        if (saved) {
          setBaseUrl(saved.baseUrl);
          setModelClaimant(saved.modelClaimant);
          setModelDefendant(saved.modelDefendant);
          setModelJudge(saved.modelJudge);
          setJurisdiction(saved.jurisdiction);
          setMaxRounds(saved.maxRounds);
          setTargetSide(saved.targetSide);
        } else if (defaults.config_found && defaults.base_url) {
          setBaseUrl(defaults.base_url);
          if (defaults.model_claimant_lawyer) setModelClaimant(defaults.model_claimant_lawyer);
          if (defaults.model_defendant_lawyer) setModelDefendant(defaults.model_defendant_lawyer);
          if (defaults.model_judge) setModelJudge(defaults.model_judge);
          if (defaults.jurisdiction) setJurisdiction(defaults.jurisdiction);
          setMaxRounds(defaults.max_rounds);
        }
        setDefaultsLoaded(true);
      })
      .catch(() => !cancelled && setDefaultsLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  /** Компактный режим доступен, если конфиг полон и ключ есть в .env. */
  const compactConfigAvailable =
    !!configDefaults &&
    configDefaults.config_found &&
    !!configDefaults.base_url &&
    !!configDefaults.model_claimant_lawyer &&
    !!configDefaults.model_defendant_lawyer &&
    !!configDefaults.model_judge &&
    configDefaults.has_env_key;

  const canStart = files.length > 0 || context.trim().length > 0;

  const runDebate = useCallback(
    async (sessionId: string) => {
      setStage("live");
      setMessages([]);
      setReport(null);
      setTyping(false);
      await startRun(sessionId);
      connectSessionSocket(
        sessionId,
        (event: DebateEvent) => {
          if (event.type === "agent_start") {
            setTyping(true);
            setMessages((prev) => [
              ...prev,
              {
                id: nextId.current++,
                speakerTitle: event.speaker_title ?? event.role ?? "",
                role: event.role ?? "",
                round: event.round ?? 0,
                model: event.model ?? "",
                text: "",
                done: false,
              },
            ]);
            scrollFeed();
          } else if (event.type === "delta") {
            setTyping(true);
            setMessages((prev) => {
              if (!prev.length) return prev;
              const updated = [...prev];
              const last = updated[updated.length - 1];
              updated[updated.length - 1] = { ...last, text: last.text + (event.text ?? "") };
              return updated;
            });
            scrollFeed();
          } else if (event.type === "agent_end") {
            setTyping(false);
            setMessages((prev) => {
              if (!prev.length) return prev;
              const updated = [...prev];
              const last = updated[updated.length - 1];
              updated[updated.length - 1] = { ...last, text: event.text ?? last.text, done: true };
              return updated;
            });
          } else if (event.type === "judge_decision") {
            const verb = event.continues ? "ПРОДОЛЖАТЬ" : "ЗАВЕРШИТЬ";
            setMessages((prev) => [
              ...prev,
              {
                id: nextId.current++,
                speakerTitle: "Решение судьи",
                role: "system",
                round: event.round ?? 0,
                model: "",
                text: `${verb}${event.addressee && event.addressee !== "both" ? ` → ${event.addressee}` : ""}`,
                done: true,
              },
            ]);
          } else if (event.type === "error") {
            setError(String(event.payload?.message ?? "ошибка симуляции"));
          }
        },
        async () => {
          // Поток закрыт: забираем отчёт (409 = сессия завершилась ошибкой)
          try {
            const data = await fetchReport(sessionId);
            setReport(data);
            setStage("final");
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
          }
        },
      );
    },
    [scrollFeed],
  );

  /** Добавить файлы (drag-and-drop или проводник) с клиентской фильтрацией. */
  const addFiles = useCallback((incoming: FileList | File[]) => {
    const allowed = [".pdf", ".docx", ".txt", ".md"];
    const skipped: string[] = [];
    setFiles((prev) => {
      const seen = new Set(prev.map((file) => `${file.name}:${file.size}`));
      const next = [...prev];
      for (const file of Array.from(incoming)) {
        const name = file.name.toLowerCase();
        if (!allowed.some((ext) => name.endsWith(ext))) {
          skipped.push(file.name);
          continue;
        }
        const key = `${file.name}:${file.size}`;
        if (seen.has(key)) continue; // дедуп: тот же файл дважды
        seen.add(key);
        next.push(file);
      }
      return next;
    });
    setClientFileError(
      skipped.length ? `Пропущены неподдерживаемые файлы: ${skipped.join(", ")}` : null,
    );
  }, []);

  const handleSetup = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const settings: SavedSettings = {
        baseUrl,
        modelClaimant,
        modelDefendant,
        modelJudge,
        jurisdiction,
        maxRounds,
        targetSide,
      };
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
      sessionIdRef.current = await createSession({
        base_url: baseUrl,
        api_key: apiKey,
        model_claimant_lawyer: modelClaimant,
        model_defendant_lawyer: modelDefendant,
        model_judge: modelJudge,
        jurisdiction,
        max_rounds: maxRounds,
        target_side: targetSide,
        llm_params: configDefaults?.llm_params ?? { reasoning: { effort: "low" } },
      });
      setStage("upload");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [baseUrl, apiKey, modelClaimant, modelDefendant, modelJudge, jurisdiction, maxRounds, targetSide, configDefaults]);

  const handleUpload = useCallback(async () => {
    const sessionId = sessionIdRef.current;
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    try {
      const result = await uploadCase(sessionId, files, context);
      if (!result.saved_files.length && !result.context_saved) {
        setError("Загрузите хотя бы один документ или опишите ситуацию текстом.");
        return;
      }
      const parts: string[] = [];
      if (result.saved_files.length) parts.push(`документы: ${result.saved_files.join(", ")}`);
      if (result.context_saved) parts.push("контекст дела сохранён");
      if (result.rejected_files.length)
        parts.push(`отклонены: ${result.rejected_files.map((f) => f.file).join(", ")}`);
      setUploadSummary(parts.join("; "));
      await runDebate(sessionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [files, context, runDebate]);

  const stepBadge = (step: number, label: string) => {
    const order = { setup: 1, upload: 2, live: 3, final: 4 } as const;
    const current = order[stage];
    const active = step === current;
    const doneStep = step < current;
    return (
      <div className="flex items-center gap-2">
        <span
          className={`flex h-7 w-7 items-center justify-center rounded-full text-sm font-semibold ${
            doneStep
              ? "bg-emerald-500 text-white"
              : active
                ? "bg-blue-600 text-white"
                : "bg-slate-300 text-slate-600"
          }`}
        >
          {doneStep ? "✓" : step}
        </span>
        <span className={active ? "font-semibold" : "text-slate-500"}>{label}</span>
      </div>
    );
  };

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-8">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 no-print">
        <h1 className="text-2xl font-bold">⚖ Судебный симулятор</h1>
        <div className="flex flex-wrap gap-4 text-sm">
          {stepBadge(1, "Настройка")}
          {stepBadge(2, "Загрузка дела")}
          {stepBadge(3, "Прения")}
          {stepBadge(4, "Итог")}
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {/* --- ЭКРАН 1: НАСТРОЙКА --- */}
      {stage === "setup" && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-1 text-lg font-semibold">Настройка</h2>
          {compactConfigAvailable && !showFullForm && (
            <div className="mt-3">
              <p className="mb-5 text-sm text-slate-500">
                Конфигурация загружена из config.yaml и .env — можно сразу переходить к делу.
              </p>
              <div className="mb-5 space-y-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm">
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="text-slate-600">Роутер</span>
                  <span className="font-mono text-xs">{configDefaults!.base_url}</span>
                </div>
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="text-slate-600">Модели (заявитель / ответчик / судья)</span>
                  <span className="font-mono text-xs">
                    {configDefaults!.model_claimant_lawyer === configDefaults!.model_defendant_lawyer &&
                    configDefaults!.model_claimant_lawyer === configDefaults!.model_judge
                      ? configDefaults!.model_claimant_lawyer
                      : `${configDefaults!.model_claimant_lawyer} / ${configDefaults!.model_defendant_lawyer} / ${configDefaults!.model_judge}`}
                  </span>
                </div>
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="text-slate-600">Юрисдикция</span>
                  <span>{configDefaults!.jurisdiction}</span>
                </div>
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="text-slate-600">Ключ API</span>
                  <span className="font-mono text-xs">
                    из .env ({configDefaults!.api_key_env}): {configDefaults!.api_key_masked}
                  </span>
                </div>
              </div>
              <div className="mb-4 grid gap-4 sm:grid-cols-2">
                <label className="text-sm">
                  <span className="mb-1 block font-medium">Раундов прений (максимум)</span>
                  <input
                    type="number"
                    min={1}
                    max={10}
                    className="w-full rounded-lg border border-slate-300 px-3 py-2"
                    value={maxRounds}
                    onChange={(e) => setMaxRounds(Math.min(10, Math.max(1, Number(e.target.value) || 1)))}
                  />
                </label>
                <div className="text-sm">
                  <span className="mb-1 block font-medium">Рекомендации для стороны</span>
                  <div className="flex gap-3">
                    {(
                      [
                        ["claimant", "Заявителя"],
                        ["defendant", "Ответчика"],
                      ] as [TargetSide, string][]
                    ).map(([side, label]) => (
                      <button
                        key={side}
                        type="button"
                        onClick={() => setTargetSide(side)}
                        className={`flex-1 rounded-lg border px-3 py-2 transition ${
                          targetSide === side
                            ? "border-blue-600 bg-blue-50 font-semibold text-blue-800"
                            : "border-slate-300 bg-white hover:bg-slate-50"
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setShowFullForm(true)}
                className="text-sm font-medium text-blue-600 hover:underline"
              >
                Изменить конфигурацию вручную
              </button>
              <button
                type="button"
                disabled={busy || !defaultsLoaded}
                onClick={handleSetup}
                className="mt-4 w-full rounded-lg bg-blue-600 px-4 py-2.5 font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50"
              >
                {busy ? "Создаю сессию…" : "Дальше: загрузить дело"}
              </button>
            </div>
          )}
          {!(compactConfigAvailable && !showFullForm) && (
          <>
          <p className="mb-5 text-sm text-slate-500">
            Роутер, модели агентов и юрисдикция. Ключ можно не вводить — он подставится из .env.
          </p>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-sm sm:col-span-2">
              <span className="mb-1 block font-medium">API роутера ([OI]-совместимый)</span>
              <input
                className="w-full rounded-lg border border-slate-300 px-3 py-2"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://routerai.ru/api/v1"
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block font-medium">
                Ключ API
                {configDefaults?.has_env_key && (
                  <span className="ml-2 font-normal text-emerald-700">
                    есть в .env ({configDefaults.api_key_masked}) — можно оставить пустым
                  </span>
                )}
              </span>
              <input
                type="password"
                className="w-full rounded-lg border border-slate-300 px-3 py-2"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={configDefaults?.has_env_key ? "используется ключ из .env" : "sk-…"}
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block font-medium">Раундов прений (максимум)</span>
              <input
                type="number"
                min={1}
                max={10}
                className="w-full rounded-lg border border-slate-300 px-3 py-2"
                value={maxRounds}
                onChange={(e) => setMaxRounds(Math.min(10, Math.max(1, Number(e.target.value) || 1)))}
              />
            </label>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            {[
              { label: "Модель юриста заявителя", value: modelClaimant, set: setModelClaimant },
              { label: "Модель юриста ответчика", value: modelDefendant, set: setModelDefendant },
              { label: "Модель судьи и аналитика", value: modelJudge, set: setModelJudge },
            ].map((field) => (
              <label key={field.label} className="text-sm">
                <span className="mb-1 block font-medium">{field.label}</span>
                <select
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2"
                  value={field.value}
                  onChange={(e) => field.set(e.target.value)}
                >
                  {MODEL_PRESETS.map((preset) => (
                    <option key={preset.value} value={preset.value}>
                      {preset.label}
                    </option>
                  ))}
                  {!MODEL_PRESETS.some((preset) => preset.value === field.value) && (
                    <option value={field.value}>{field.value}</option>
                  )}
                </select>
              </label>
            ))}
            <label className="text-sm">
              <span className="mb-1 block font-medium">Юрисдикция (можно свой вариант)</span>
              <input
                list="jurisdiction-presets"
                className="w-full rounded-lg border border-slate-300 px-3 py-2"
                value={jurisdiction}
                onChange={(e) => setJurisdiction(e.target.value)}
              />
              <datalist id="jurisdiction-presets">
                {JURISDICTIONS.map((item) => (
                  <option key={item} value={item} />
                ))}
              </datalist>
            </label>
          </div>
          <div className="mt-4 text-sm">
            <span className="mb-1 block font-medium">Рекомендации готовить для стороны</span>
            <div className="flex gap-3">
              {(
                [
                  ["claimant", "Заявителя (истца)"],
                  ["defendant", "Ответчика"],
                ] as [TargetSide, string][]
              ).map(([side, label]) => (
                <button
                  key={side}
                  type="button"
                  onClick={() => setTargetSide(side)}
                  className={`flex-1 rounded-lg border px-3 py-2 transition ${
                    targetSide === side
                      ? "border-blue-600 bg-blue-50 font-semibold text-blue-800"
                      : "border-slate-300 bg-white hover:bg-slate-50"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={handleSetup}
            className="mt-6 w-full rounded-lg bg-blue-600 px-4 py-2.5 font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50"
          >
            {busy ? "Создаю сессию…" : "Дальше: загрузить дело"}
          </button>
          </>
          )}
        </section>
      )}

      {/* --- ЭКРАН 2: ЗАГРУЗКА ДЕЛА --- */}
      {stage === "upload" && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-1 text-lg font-semibold">Загрузка дела</h2>
          <p className="mb-5 text-sm text-slate-500">
            Опишите ситуацию текстом и приложите документы (PDF, DOCX, TXT, MD). Агенты будут
            опираться только на эти материалы.
          </p>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              addFiles(e.dataTransfer.files);
            }}
            className={`mb-4 rounded-xl border-2 border-dashed px-6 py-10 text-center transition ${
              dragOver ? "border-blue-500 bg-blue-50" : "border-slate-300 bg-slate-50"
            }`}
          >
            <p className="font-medium">Перетащите файлы сюда</p>
            <p className="mt-1 text-sm text-slate-500">PDF · DOCX · TXT · MD</p>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="mt-3 text-sm font-medium text-blue-600 hover:underline"
            >
              или выберите на диске
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.md"
              className="hidden"
              onChange={(e) => {
                if (e.target.files) addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            {clientFileError && (
              <p className="mt-3 text-sm text-red-600">{clientFileError}</p>
            )}
            {files.length > 0 && (
              <ul className="mt-4 flex flex-wrap justify-center gap-2 text-xs">
                {files.map((file, index) => (
                  <li
                    key={`${file.name}-${index}`}
                    className="flex items-center gap-2 rounded-full border border-slate-300 bg-white px-3 py-1"
                  >
                    📎 {file.name}
                    <button
                      type="button"
                      className="text-slate-400 hover:text-red-600"
                      onClick={() => setFiles((prev) => prev.filter((_, i) => i !== index))}
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <label className="text-sm">
            <span className="mb-1 block font-medium">Описание ситуации / промпт-контекст</span>
            <textarea
              rows={8}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs"
              value={context}
              onChange={(e) => setContext(e.target.value)}
              placeholder="Суть спора, стороны, позиции, что должно решить судье…"
            />
          </label>
          {uploadSummary && <p className="mt-3 text-sm text-emerald-700">{uploadSummary}</p>}
          {clientFileError && !files.length && (
            <p className="mb-3 text-sm text-red-600">{clientFileError}</p>
          )}
          <button
            type="button"
            disabled={busy || !canStart}
            onClick={handleUpload}
            className="mt-6 w-full rounded-lg bg-blue-600 px-4 py-2.5 font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50"
          >
            {busy ? "Запускаю прения…" : "Начать прения"}
          </button>
          {!canStart && (
            <p className="mt-2 text-center text-xs text-slate-400">
              Загрузите хотя бы один документ или опишите ситуацию текстом
            </p>
          )}
          <button
            type="button"
            onClick={() => setStage("setup")}
            className="mt-3 w-full rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
          >
            ← Назад к настройке
          </button>
        </section>
      )}

      {/* --- ЭКРАН 3: ПРЯМОЙ ЭФИР --- */}
      {stage === "live" && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
            <span className="text-sm font-medium">Прямой эфир прений</span>
            {typing && (
              <span className="flex items-center gap-2 text-sm text-slate-500">
                <span className="flex gap-1">
                  <span className="h-2 w-2 animate-bounce rounded-full bg-slate-400" />
                  <span className="h-2 w-2 animate-bounce rounded-full bg-slate-400 [animation-delay:150ms]" />
                  <span className="h-2 w-2 animate-bounce rounded-full bg-slate-400 [animation-delay:300ms]" />
                </span>
                печатает…
              </span>
            )}
          </div>
          <div ref={feedRef} className="flex max-h-[70vh] flex-col gap-3 overflow-y-auto pr-1">
            {messages.map((message) => (
              <article
                key={message.id}
                className={`rounded-xl border px-4 py-3 shadow-sm ${
                  ROLE_STYLES[message.role] ?? "border-emerald-300 bg-emerald-50"
                }`}
              >
                <header className="mb-1 flex flex-wrap items-baseline gap-x-3 text-xs text-slate-600">
                  <span className="text-sm font-semibold text-slate-900">{message.speakerTitle}</span>
                  <span>раунд {message.round}</span>
                  {message.model && <span className="font-mono">{message.model}</span>}
                </header>
                <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.text}</p>
              </article>
            ))}
          </div>
        </section>
      )}

      {/* --- ЭКРАН 4: ИТОГ --- */}
      {stage === "final" && report && (
        <section className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3 no-print">
            <h2 className="text-lg font-semibold">Итог</h2>
            <div className="flex gap-2">
              <a
                href={reportDownloadUrl(report.session_id)}
                className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium hover:bg-slate-50"
              >
                ⬇ Скачать отчёт (.md)
              </a>
              <button
                type="button"
                onClick={() => window.print()}
                className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium hover:bg-slate-50"
              >
                🖨 Печать / PDF
              </button>
              <button
                type="button"
                onClick={() => {
                  setStage("setup");
                  setReport(null);
                  setUploadSummary(null);
                }}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
              >
                Новое дело
              </button>
            </div>
          </div>

          {report.legal_warning && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              ⚠ {report.legal_warning} — ссылки модели на нормы требуют проверки.
            </div>
          )}

          <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm">
            Раундов: {report.params.rounds_played} · юрисдикция: {report.params.jurisdiction}
            {report.verified_norms.length > 0 && (
              <> · норм права подтверждено: {report.verified_norms.length}</>
            )}
          </div>

          {report.recommendations && (
            <article className="rounded-xl border-2 border-emerald-500 bg-emerald-50 p-5 shadow-sm">
              <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-base font-bold text-emerald-900">
                  💡 Рекомендации для стороны: {report.recommendations.side_title}
                </h3>
                <span className="rounded-full bg-emerald-600 px-3 py-0.5 text-xs font-semibold text-white">
                  перспектива: {report.recommendations.prospects}
                </span>
              </div>
              <p className="mb-3 text-xs text-emerald-800">
                Подготовлено ИИ-аналитиком для подготовки к спору; не заменяет юриста.
              </p>
              <Markdown text={report.recommendations.text} />
            </article>
          )}

          <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h3 className="mb-3 text-base font-bold">Решение судьи</h3>
            <Markdown text={report.verdict} />
          </article>

          {report.verified_norms.length > 0 && (
            <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <h3 className="mb-2 text-base font-bold">Подтверждённые нормы права (pravo.gov.ru)</h3>
              <ul className="space-y-2 text-sm">
                {report.verified_norms.map((norm) => (
                  <li key={norm.url}>
                    {norm.title}
                    {norm.date && <> · {norm.date.slice(0, 10)}</>}{" "}
                    <a
                      className="text-blue-600 hover:underline"
                      href={norm.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      публикация ↗
                    </a>
                  </li>
                ))}
              </ul>
            </article>
          )}

          <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h3 className="mb-3 text-base font-bold">Прения (протокол)</h3>
            <div className="space-y-4">
              {report.history.map((statement, index) => (
                <div key={index}>
                  <p className="text-xs font-semibold text-slate-500">
                    {statement.speaker_title} · раунд {statement.round}
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{statement.text}</p>
                </div>
              ))}
            </div>
          </article>
        </section>
      )}

      <footer className="mt-8 text-center text-xs text-slate-400 no-print">
        ИИ-инструмент подготовки к спору. Не заменяет консультацию практикующего юриста.
      </footer>
    </main>
  );
}

