import type {
  AgentReply,
  DispatchReply,
  DistrictCollection,
  FlowPathCollection,
  HouseholdAlert,
  HouseholdResponse,
  HouseholdsReply,
  HouseholdTier,
  IntakeLayer,
  OperatorCase,
  PasivosLayer,
  SiteDetail,
  SitesResponse,
} from "./types";

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`GET ${path} failed with HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const fetchSites = (): Promise<SitesResponse> => getJson<SitesResponse>("/sites?detail=true");

export const fetchSiteDetail = (id: string): Promise<SiteDetail> =>
  getJson<SiteDetail>(`/sites/${encodeURIComponent(id)}`);

export const fetchFlowPaths = (): Promise<FlowPathCollection> => getJson<FlowPathCollection>("/flowpaths");

export const fetchIntakes = (): Promise<IntakeLayer> => getJson<IntakeLayer>("/layers/intakes");

/** Only available with real data; the caller treats a failure as "layer not available". */
export const fetchDistricts = (): Promise<DistrictCollection> => getJson<DistrictCollection>("/districts");

async function postJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { method: "POST", ...init });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // keep the HTTP status as the detail
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const askAgentText = (text: string, previousResponseId: string | null): Promise<AgentReply> =>
  postJson<AgentReply>("/agent/text", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, previous_response_id: previousResponseId }),
  });

export function askAgentVoice(wav: Blob, previousResponseId: string | null): Promise<AgentReply> {
  const form = new FormData();
  form.append("audio", wav, "question.wav");
  if (previousResponseId) form.append("previous_response_id", previousResponseId);
  return postJson<AgentReply>("/agent/voice", { body: form });
}

export const fetchPasivos = (): Promise<PasivosLayer> => getJson<PasivosLayer>("/layers/pasivos");

// --- alert loop ----------------------------------------------------------------------

export const dispatchAlerts = (
  siteId: string,
  tier: HouseholdTier,
  operatorConfirmationId: string | null,
): Promise<DispatchReply> =>
  postJson<DispatchReply>("/alerts/dispatch", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site_id: siteId, tier, operator_confirmation_id: operatorConfirmationId }),
  });

export const fetchHouseholdAlerts = (): Promise<HouseholdsReply> => getJson<HouseholdsReply>("/alerts/households");

export const fetchCases = (): Promise<{ cases: OperatorCase[] }> => getJson<{ cases: OperatorCase[] }>("/alerts/cases");

/** A WhatsApp-style reply typed on the simulated phone (1 = SAFE, 2 = NEEDS_HELP). */
export const sendSimulatorReply = (
  alertId: string,
  response: HouseholdResponse,
): Promise<{ alert: HouseholdAlert; cases: OperatorCase[] }> =>
  postJson<{ alert: HouseholdAlert; cases: OperatorCase[] }>(`/alerts/${encodeURIComponent(alertId)}/response`, {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ response, channel: "whatsapp" }),
  });
