"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { DebateEvent, DefaultsData, TargetSide } from "@/lib/api";
import { API_BASE, checkHealth, connectSessionSocket, createSession, currencySymbol, fetchDefaults, fetchEvidence, fetchReport, reportDownloadUrl, startRun, stopDebate, uploadCase } from "@/lib/api";

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
  { label: "Qwen — средний китаец", value: "qwen/qwen3.8-max-0902" },
  { label: "DeepSeek — другой китаец", value: "~deepseek/deepseek-v4-pro-latest" },
  { label: "Grok", value: "~x-ai/grok-latest" },
  { label: "Claude Sonnet (последний)", value: "~anthropic/claude-sonnet-latest" },
  { label: "Claude Opus (последний)", value: "~anthropic/claude-opus-latest" },
  { label: "Kimi (последний)", value: "~moonshotai/kimi-latest" },
  { label: "GPT-6 Astra (богоподобная)", value: "openai/gpt-6-astra" },
];

/** Ключ localStorage с пользовательскими алиасами моделей. */
const CUSTOM_MODELS_KEY = "court-sim-custom-models-v1";

function loadCustomModels(): string[] {
  try {
    const raw = window.localStorage.getItem(CUSTOM_MODELS_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(parsed) ? parsed.filter((m): m is string => typeof m === "string") : [];
  } catch {
    return [];
  }
}

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

/** Инлайн-форматирование: **жирный**, `код`, ссылки [текст](url). */
function InlineText({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
          return (
            <code key={index} className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.9em]">
              {part.slice(1, -1)}
            </code>
          );
        }
        const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (link) {
          return (
            <a
              key={index}
              href={link[2]}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline"
            >
              {link[1]} ↗
            </a>
          );
        }
        return <span key={index}>{part}</span>;
      })}
    </>
  );
}

/** Простой markdown-рендер: заголовки, списки, абзацы + инлайн-формат. */
function Markdown({ text }: { text: string }) {
  const blocks = text.trim().split(/\n{2,}/);
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      {blocks.map((block, index) => {
        const lines = block.split("\n").filter((line) => line.trim());
        if (!lines.length) return null;
        // Заголовки: строка блока начинается с #
        if (lines[0].trimStart().startsWith("#")) {
          const level = (lines[0].trimStart().match(/^#+/) ?? ["#"])[0].length;
          const content = lines.map((line) => line.replace(/^#+\s*/, "")).join(" · ");
          const size =
            level === 1
              ? "text-base font-bold"
              : level === 2
                ? "text-sm font-semibold"
                : "text-sm font-semibold text-slate-700";
          return (
            <h3 key={index} className={size}>
              <InlineText text={content} />
            </h3>
          );
        }
        // Списки: - / * / 1.
        if (lines.every((line) => /^\s*([-*•]|\d+[.)])\s+/.test(line))) {
          const ordered = /^\s*\d/.test(lines[0]);
          const items = lines.map((line) => line.replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
          const ListTag = ordered ? "ol" : "ul";
          return (
            <ListTag
              key={index}
              className={`space-y-1 pl-5 ${ordered ? "list-decimal" : "list-disc"}`}
            >
              {items.map((item, i) => (
                <li key={i}>
                  <InlineText text={item} />
                </li>
              ))}
            </ListTag>
          );
        }
        return (
          <p key={index}>
            <InlineText text={block} />
          </p>
        );
      })}
    </div>
  );
}

/** Селектор модели с пресетами, пользовательскими алиасами и пунктом «свой алиас». */
function ModelSelect({
  label,
  value,
  onChange,
  customModels,
  onStartAlias,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  customModels: string[];
  onStartAlias: () => void;
}) {
  const allValues = [...MODEL_PRESETS.map((p) => p.value), ...customModels];
  const known = allValues.includes(value);
  return (
    <label className="text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      <select
        className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2"
        value={known ? value : ""}
        onChange={(e) => {
          if (e.target.value === "__custom__") {
            onStartAlias();
          } else if (e.target.value) {
            onChange(e.target.value);
          }
        }}
      >
        {!known && value && <option value="">{value} (текущая, не из списка)</option>}
        {MODEL_PRESETS.map((preset, index) => (
          // Дубли значений (два the model) различаем по индексу.
          <option key={`${preset.value}-${index}`} value={preset.value}>
            {preset.label}
          </option>
        ))}
        {customModels.map((model) => (
          <option key={model} value={model}>
            ★ {model}
          </option>
        ))}
        <option value="__custom__">✎ свой алиас…</option>
      </select>
    </label>
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
  // Пользовательские алиасы моделей (общий список для всех трёх ролей).
  const [customModels, setCustomModels] = useState<string[]>([]);
  const [aliasRole, setAliasRole] = useState<"claimant" | "defendant" | "judge" | null>(null);
  const [aliasValue, setAliasValue] = useState("");

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
  // Живой счётчик расхода: суммарные токены за прогон (обновляется по agent_end).
  const [liveUsage, setLiveUsage] = useState({ tokens: 0, calls: 0 });
  const socketRef = useRef<WebSocket | null>(null);

  // --- итог ---
  const [report, setReport] = useState<Awaited<ReturnType<typeof fetchReport>> | null>(null);
  const [evidence, setEvidence] = useState<Awaited<ReturnType<typeof fetchEvidence>>>(null);
  const sessionIdRef = useRef<string | null>(null);
  // Живость бэкенда (индикатор в шапке).
  const [backendUp, setBackendUp] = useState<boolean | null>(null);

  const scrollFeed = useCallback(() => {
    requestAnimationFrame(() => {
      if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
    });
  }, []);

  // При открытии страницы: преднастройки из config.yaml/.env + сохранённая форма.
  useEffect(() => {
    setCustomModels(loadCustomModels());
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

  // Индикатор бэкенда: проверяем сейчас и далее раз в 5 секунд.
  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      checkHealth().then((ok) => !cancelled && setBackendUp(ok));
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const runDebate = useCallback(
    async (sessionId: string) => {
      setStage("live");
      setMessages([]);
      setReport(null);
      setTyping(false);
      setLiveUsage({ tokens: 0, calls: 0 });
      await startRun(sessionId);
      const socket = connectSessionSocket(
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
            // Живой счётчик расхода токенов (usage из payload agent_end).
            const usage = (event.payload?.usage ?? null) as
              | { total_tokens?: number }
              | null;
            if (usage?.total_tokens) {
              setLiveUsage((prev) => ({
                tokens: prev.tokens + (usage.total_tokens ?? 0),
                calls: prev.calls + 1,
              }));
            }
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
          // Поток закрыт: забираем отчёт (409 = сессия завершилась ошибкой/остановлена)
          socketRef.current = null;
          try {
            const data = await fetchReport(sessionId);
            setReport(data);
            setStage("final");
            // Evidence Pack — параллельно; null, если ещё не собран.
            fetchEvidence(sessionId).then(setEvidence).catch(() => setEvidence(null));
          } catch {
            // Сессия остановлена пользователем — возвращаемся к материалам дела.
            setStage("upload");
          }
        },
      );
      socketRef.current = socket;
    },
    [scrollFeed],
  );

  /** Остановить генерацию и вернуться к материалам дела. */
  const handleStop = useCallback(async () => {
    const sessionId = sessionIdRef.current;
    if (!sessionId) return;
    setBusy(true);
    try {
      await stopDebate(sessionId); // сессия перейдёт в stopped, WS закроется сам
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, []);

  /** Добавить файлы (drag-and-drop или проводник) с клиентской фильтрацией. */
  const addFiles = useCallback((incoming: File[]) => {
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
        <div className="flex flex-wrap items-center gap-4 text-sm">
          {stepBadge(1, "Настройка")}
          {stepBadge(2, "Загрузка дела")}
          {stepBadge(3, "Прения")}
          {stepBadge(4, "Итог")}
        </div>
      </div>

      {backendUp === false && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900 no-print">
          <span className="h-3 w-3 shrink-0 rounded-full bg-red-500" />
          <span>
            Бэкенд не отвечает ({API_BASE}). Запустите <b>install.bat</b> (или окно «court-sim
            backend»: <code>.venv\Scripts\python.exe -m uvicorn src.api:app --port 8000</code>) и
            подождите пару секунд — индикатор станет зелёным.
          </span>
        </div>
      )}

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
            <ModelSelect
              label="Модель юриста заявителя"
              value={modelClaimant}
              onChange={setModelClaimant}
              customModels={customModels}
              onStartAlias={() => {
                setAliasRole("claimant");
                setAliasValue("");
              }}
            />
            <ModelSelect
              label="Модель юриста ответчика"
              value={modelDefendant}
              onChange={setModelDefendant}
              customModels={customModels}
              onStartAlias={() => {
                setAliasRole("defendant");
                setAliasValue("");
              }}
            />
            <ModelSelect
              label="Модель судьи и аналитика"
              value={modelJudge}
              onChange={setModelJudge}
              customModels={customModels}
              onStartAlias={() => {
                setAliasRole("judge");
                setAliasValue("");
              }}
            />
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
          {aliasRole !== null && (
            <div className="mt-4 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3">
              <label className="text-sm">
                <span className="mb-1 block font-medium">
                  Свой алиас модели для{" "}
                  {aliasRole === "claimant"
                    ? "юриста заявителя"
                    : aliasRole === "defendant"
                      ? "юриста ответчика"
                      : "судьи/аналитика"}
                </span>
                <div className="flex gap-2">
                  <input
                    autoFocus
                    className="flex-1 rounded-lg border border-blue-300 px-3 py-2 font-mono text-xs"
                    value={aliasValue}
                    onChange={(e) => setAliasValue(e.target.value)}
                    placeholder="например: ~vendor/model-latest"
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setAliasRole(null);
                    }}
                  />
                  <button
                    type="button"
                    disabled={!aliasValue.trim()}
                    onClick={() => {
                      const alias = aliasValue.trim();
                      if (!alias) return;
                      const next = customModels.includes(alias)
                        ? customModels
                        : [...customModels, alias];
                      setCustomModels(next);
                      window.localStorage.setItem(CUSTOM_MODELS_KEY, JSON.stringify(next));
                      if (aliasRole === "claimant") setModelClaimant(alias);
                      else if (aliasRole === "defendant") setModelDefendant(alias);
                      else setModelJudge(alias);
                      setAliasRole(null);
                    }}
                    className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
                  >
                    Добавить
                  </button>
                  <button
                    type="button"
                    onClick={() => setAliasRole(null)}
                    className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-600 hover:bg-slate-50"
                  >
                    Отмена
                  </button>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  Алиас сохранится в списке (★) и будет доступен во всех трёх селектах.
                </p>
              </label>
            </div>
          )}
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
              // ВАЖНО: копируем FileList синхронно — иначе список умирает вместе с событием.
              addFiles(Array.from(e.dataTransfer.files));
            }}
            className={`mb-4 rounded-xl border-2 border-dashed px-6 py-10 text-center transition ${
              dragOver ? "border-blue-500 bg-blue-50" : "border-slate-300 bg-slate-50"
            }`}
          >
            <p className="font-medium">Перетащите файлы сюда</p>
            <p className="mt-1 text-sm text-slate-500">PDF · DOCX · TXT · MD</p>
            <label className="mt-3 inline-block cursor-pointer text-sm font-medium text-blue-600 hover:underline">
              или выберите на диске
              <input
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.md"
                onChange={(e) => {
                  // ВАЖНО: копия до сброса value — FileList инвалидируется сразу после.
                  const picked = Array.from(e.target.files ?? []);
                  e.target.value = "";
                  addFiles(picked);
                }}
                className="sr-only"
              />
            </label>
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
            <div className="flex items-center gap-4">
              <span className="text-sm font-medium">Прямой эфир прений</span>
              {liveUsage.calls > 0 && (
                <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
                  🪙 {liveUsage.tokens.toLocaleString("ru-RU")} токенов · {liveUsage.calls} вызовов
                </span>
              )}
            </div>
            <div className="flex items-center gap-3">
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
              <button
                type="button"
                disabled={busy}
                onClick={handleStop}
                className="rounded-lg border border-red-300 bg-red-50 px-4 py-2 text-sm font-medium text-red-700 transition hover:bg-red-100 disabled:opacity-50"
              >
                ⏹ Остановить
              </button>
            </div>
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
                {message.text ? (
                  <Markdown text={message.text} />
                ) : (
                  <p className="text-sm text-slate-400">генерирует…</p>
                )}
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
                  setFiles([]);
                  setContext("");
                  setError(null);
                  setClientFileError(null);
                  setMessages([]);
                  sessionIdRef.current = null;
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

          {report.stopped && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              ⚠ Симуляция была остановлена вами — решение неполное.
            </div>
          )}

          {report.cost_summary && report.cost_summary.calls.length > 0 && (
            <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-base font-bold">Потребление ресурсов</h3>
                <span className="text-sm text-slate-600">
                  🪙 {report.cost_summary.totals.total_tokens.toLocaleString("ru-RU")} токенов
                  {report.cost_summary.cost_available && report.cost_summary.total_cost_usd !== null && (
                    <> · 💰 ≈ {currencySymbol(report.cost_summary.currency)}{report.cost_summary.total_cost_usd.toFixed(4)}</>
                  )}
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-slate-200 text-left text-slate-500">
                      <th className="py-1.5 pr-3 font-medium">#</th>
                      <th className="py-1.5 pr-3 font-medium">Роль</th>
                      <th className="py-1.5 pr-3 font-medium">Вход</th>
                      <th className="py-1.5 pr-3 font-medium">Выход</th>
                      <th className="py-1.5 pr-3 font-medium">Кэш</th>
                      <th className="py-1.5 pr-3 font-medium">Всего</th>
                      <th className="py-1.5 font-medium">Цена</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.cost_summary.calls.map((call, index) => (
                      <tr key={index} className="border-b border-slate-100">
                        <td className="py-1.5 pr-3">{index + 1}</td>
                        <td className="py-1.5 pr-3">{call.role}</td>
                        <td className="py-1.5 pr-3">{call.usage.input_tokens}</td>
                        <td className="py-1.5 pr-3">{call.usage.output_tokens}</td>
                        <td className="py-1.5 pr-3">{call.usage.cached_tokens}</td>
                        <td className="py-1.5 pr-3 font-medium">{call.usage.total_tokens}</td>
                        <td className="py-1.5">
                          {call.cost_usd !== null && report.cost_summary ? `${currencySymbol(report.cost_summary.currency)}${call.cost_usd.toFixed(5)}` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!report.cost_summary.cost_available && (
                <p className="mt-2 text-xs text-slate-400">
                  Цены от роутера не получены — стоимость недоступна, показаны только токены.
                </p>
              )}
            </article>
          )}

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

          {evidence && evidence.sources.length > 0 && (
            <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <h3 className="mb-1 text-base font-bold">Правовые источники (Evidence Pack)</h3>
              <p className="mb-3 text-xs text-slate-500">
                Подтверждено: {evidence.sources.filter((s) => s.verification_status === "verified").length} ·
                частично: {evidence.sources.filter((s) => s.verification_status === "partially_verified").length} ·
                непроверено: {evidence.sources.filter((s) => s.verification_status === "unverified").length}
                {evidence.case_law_coverage && (
                  <> · покрытие практики: {evidence.case_law_coverage.coverage === "official_only" ? "только официальный ВС РФ" : evidence.case_law_coverage.coverage === "limited" ? "ограничено" : "источники недоступны"}</>
                )}
              </p>

              {evidence.case_law_coverage && (
                <div className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
                  ⚠ {evidence.case_law_coverage.warning ?? "Покрытие поиска практики ограничено: по практике нижестоящих судов массовый поиск не выполнялся."}
                </div>
              )}

              <ul className="space-y-3 text-sm">
                {evidence.sources.map((source) => {
                  const badge =
                    source.verification_status === "verified"
                      ? { text: "подтверждён", cls: "bg-emerald-100 text-emerald-800" }
                      : source.verification_status === "partially_verified"
                        ? { text: "частично", cls: "bg-amber-100 text-amber-800" }
                        : { text: "не проверен", cls: "bg-slate-100 text-slate-600" };
                  const level = source.authority_level ? ` · уровень ${source.authority_level}` : "";
                  return (
                    <li key={source.id} className="rounded-lg border border-slate-100 p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-slate-400">[{source.id}]</span>
                        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${badge.cls}`}>
                          {badge.text}
                        </span>
                        <span className="text-xs text-slate-500">{source.source_type}{level}</span>
                      </div>
                      <p className="mt-1 font-medium">{source.citation}</p>
                      {source.title !== source.citation && (
                        <p className="text-xs text-slate-500">{source.title}</p>
                      )}
                      {source.excerpt && (
                        <p className="mt-1 line-clamp-3 text-xs text-slate-600">{source.excerpt}</p>
                      )}
                      {source.warning && (
                        <p className="mt-1 text-xs text-amber-700">⚠ {source.warning}</p>
                      )}
                      {source.official_url && (
                        <a
                          className="mt-1 inline-block rounded-md bg-slate-100 px-2 py-1 text-xs font-medium text-blue-700 hover:bg-slate-200"
                          href={source.official_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          Открыть первоисточник ↗
                        </a>
                      )}
                    </li>
                  );
                })}
              </ul>

              {evidence.provider_statuses.length > 0 && (
                <div className="mt-3 border-t border-slate-100 pt-2 text-xs text-slate-500">
                  Источники данных:{" "}
                  {evidence.provider_statuses
                    .map((p) => `${p.provider} — ${p.status}`)
                    .join("; ")}
                </div>
              )}
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
        <br />v0.4.0
      </footer>
    </main>
  );
}

