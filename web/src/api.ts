import type {
  AgentReply,
  DistrictCollection,
  FlowPathCollection,
  IntakeLayer,
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

async function postForReply(path: string, init: RequestInit): Promise<AgentReply> {
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
  return (await response.json()) as AgentReply;
}

export const askAgentText = (text: string, previousResponseId: string | null): Promise<AgentReply> =>
  postForReply("/agent/text", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, previous_response_id: previousResponseId }),
  });

export function askAgentVoice(wav: Blob, previousResponseId: string | null): Promise<AgentReply> {
  const form = new FormData();
  form.append("audio", wav, "question.wav");
  if (previousResponseId) form.append("previous_response_id", previousResponseId);
  return postForReply("/agent/voice", { body: form });
}

export const fetchPasivos = (): Promise<PasivosLayer> => getJson<PasivosLayer>("/layers/pasivos");
