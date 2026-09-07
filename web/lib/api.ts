/** Клиент API бэкенда: REST + WebSocket, общие типы событий. */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

export type TargetSide = "claimant" | "defendant";

export interface SetupPayload {
  base_url: string;
  api_key: string;
  model_claimant_lawyer: string;
  model_defendant_lawyer: string;
  model_judge: string;
  jurisdiction: string;
  max_rounds: number;
  target_side: TargetSide;
  llm_params: Record<string, unknown>;
  web_search?: boolean;
  search_model?: string | null;
}

export interface UploadResult {
  saved_files: string[];
  rejected_files: { file: string; reason: string }[];
  context_saved: boolean;
  status: string;
}

/** Событие симуляции (DebateEvent as_dict с бэкенда). */
export interface DebateEvent {
  type: string;
  role?: string;
  speaker_title?: string;
  round?: number;
  model?: string;
  text?: string;
  continues?: boolean;
  addressee?: string;
  payload?: Record<string, unknown>;
  requalify?: boolean; // судья объявил переквалификацию дела
}

// --- Правовой research layer (этап 3): payload-типы новых событий ----------

export interface ProviderStatusPayload {
  provider: string;
  status: "healthy" | "degraded" | "unavailable" | "not_configured";
  message?: string | null;
}

export interface LegalSourceFoundPayload {
  source_id: string;
  verification_status:
    | "verified"
    | "partially_verified"
    | "unverified"
    | "unavailable"
    | "contradicted";
  citation?: string;
  source_type?: string;
}

export interface EvidencePackReadyPayload {
  verified_count: number;
  partial_count: number;
  warning_count: number;
  total_sources?: number;
}

/** Карточка источника Evidence Pack (этап 3). */
export interface LegalSourceCard {
  id: string;
  source_type: string;
  title: string;
  authority: string;
  citation: string;
  excerpt: string;
  official_url: string | null;
  effective_date: string | null;
  decision_date: string | null;
  case_number: string | null;
  court: string | null;
  verified: boolean;
  verification_status:
    | "verified"
    | "partially_verified"
    | "unverified"
    | "unavailable"
    | "contradicted";
  provider: string;
  retrieved_at: string;
  relevance_score: number;
  supports_issues: string[];
  warning: string | null;
  authority_level: string | null;
}

/** Покрытие поиска судебной практики (шаг 8). */
export interface CaseLawCoverage {
  searched_sources: string[];
  not_searched_sources: string[];
  coverage: "official_only" | "limited" | "unavailable";
  warning: string | null;
}

/** Evidence Pack сессии (GET /api/session/{id}/evidence). */
export interface EvidencePackData {
  case_id: string;
  jurisdiction: string;
  generated_at: string;
  legal_issues: string[];
  sources: LegalSourceCard[];
  provider_statuses: {
    provider: string;
    status: "healthy" | "degraded" | "unavailable" | "not_configured";
    transport: string | null;
    checked_at: string;
    capabilities: string[];
    message: string | null;
  }[];
  warnings: string[];
  case_law_coverage: CaseLawCoverage | null;
}



export interface NormExcerpt {
  title: string;
  number: string;
  date: string;
  url: string;
}

export interface UsageEntry {
  role: string;
  model: string;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    reasoning_tokens: number;
    total_tokens: number;
  };
  cost_usd: number | null;
}

export interface CostSummary {
  calls: UsageEntry[];
  totals: {
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    reasoning_tokens: number;
    total_tokens: number;
  };
  total_cost_usd: number | null;
  cost_available: boolean;
  /** Валюта прайсов роутера: routerai → RUB, остальные → USD. */
  currency: "RUB" | "USD";
}

/** Символ валюты для отображения цены. */
export function currencySymbol(currency: string | undefined): string {
  return currency === "RUB" ? "₽" : "$";
}

export interface Recommendation {
  target_side: string;
  side_title: string;
  prospects: string;
  text: string;
}

export interface ReportData {
  session_id: string;
  status: string;
  target_side: string;
  params: {
    jurisdiction: string;
    models: Record<string, string>;
    max_rounds: number;
    rounds_played: number;
    finished_by_judge: boolean;
  };
  history: { speaker: string; speaker_title: string; round: number; text: string }[];
  verdict: string;
  verified_norms: NormExcerpt[];
  legal_warning: string | null;
  recommendations: Recommendation | null;
  stopped: boolean;
  cost_summary: CostSummary | null;
}

export interface DefaultsData {
  config_found: boolean;
  base_url: string;
  model_claimant_lawyer: string;
  model_defendant_lawyer: string;
  model_judge: string;
  jurisdiction: string;
  max_rounds: number;
  max_context_tokens: number;
  llm_params: Record<string, unknown>;
  api_key_env: string;
  has_env_key: boolean;
  api_key_masked: string;
}

async function ensureOk(response: Response): Promise<Response> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* тело не JSON — оставляем statusText */
    }
    throw new Error(detail);
  }
  return response;
}

/** fetch с человеческим сообщением, когда бэкенд недоступен (сеть/CORS/выключен). */
async function safeFetch(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (err) {
    if (err instanceof TypeError) {
      throw new Error(
        `Не удалось связаться с бэкендом ${API_BASE}. ` +
          "Убедитесь, что запущено окно «court-sim backend» (install.bat), " +
          "и перезагрузите страницу.",
      );
    }
    throw err;
  }
}

export async function fetchDefaults(): Promise<DefaultsData> {
  const response = await ensureOk(await safeFetch(`${API_BASE}/api/defaults`));
  return response.json();
}

export async function createSession(payload: SetupPayload): Promise<string> {
  const response = await ensureOk(await safeFetch(`${API_BASE}/api/setup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
  const data = await response.json();
  return data.session_id as string;
}

export async function uploadCase(
  sessionId: string,
  files: File[],
  context: string,
): Promise<UploadResult> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  form.append("context", context);
  const response = await ensureOk(
    await safeFetch(`${API_BASE}/api/upload/${sessionId}`, { method: "POST", body: form }),
  );
  return response.json();
}

export async function startRun(sessionId: string): Promise<void> {
  await ensureOk(await safeFetch(`${API_BASE}/api/run/${sessionId}`, { method: "POST" }));
}

/** Кооперативная остановка симуляции (флаг проверяется между LLM-вызовами). */
export async function stopDebate(sessionId: string): Promise<void> {
  await ensureOk(await safeFetch(`${API_BASE}/api/stop/${sessionId}`, { method: "POST" }));
}

export async function fetchReport(sessionId: string): Promise<ReportData> {
  const response = await ensureOk(
    await safeFetch(`${API_BASE}/api/report/${sessionId}?format=json`),
  );
  return response.json();
}

/** Evidence Pack сессии (этап 3); null — pack ещё не собран. */
export async function fetchEvidence(
  sessionId: string,
): Promise<EvidencePackData | null> {
  try {
    const response = await safeFetch(
      `${API_BASE}/api/session/${sessionId}/evidence`,
    );
    if (!response.ok) return null;
    return response.json();
  } catch {
    return null;
  }
}

/** Проверка живости бэкенда (индикатор в шапке). */
export async function checkHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/api/health`);
    return response.ok;
  } catch {
    return false;
  }
}

export function reportDownloadUrl(sessionId: string): string {
  return `${API_BASE}/api/report/${sessionId}/download`;
}

/**
 * Подключиться к потоку событий сессии. onEvent вызывается для каждого
 * события (включая replay с начала), onDone — после закрытия потока.
 */
export function connectSessionSocket(
  sessionId: string,
  onEvent: (event: DebateEvent) => void,
  onDone: () => void,
): WebSocket {
  const wsUrl = `${API_BASE.replace(/^http/, "ws")}/ws/session/${sessionId}`;
  const socket = new WebSocket(wsUrl);
  socket.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as DebateEvent);
    } catch {
      /* некорректный JSON пропускаем */
    }
  };
  socket.onclose = onDone;
  socket.onerror = onDone;
  return socket;
}
