import { apiUrl, getJson, postJson } from "./api";
import type {
  AgentConfig,
  BacktestResponse,
  DemoStatus,
  EnfenBulletin,
  GaugeValidation,
  Health,
  LearnedModelInfo,
  ProposalInfo,
  ScenarioInput,
  SignalsSnapshot,
  TraceEvent,
  TraceSnapshot,
  WatchResult,
} from "./consoleTypes";

const json = { "Content-Type": "application/json" };

export const fetchHealth = (): Promise<Health> => getJson<Health>("/health");
export const fetchAgentConfig = (): Promise<AgentConfig> => getJson<AgentConfig>("/agent/config");
export const fetchDemoStatus = (): Promise<DemoStatus> => getJson<DemoStatus>("/demo/status");

export const startAgentRun = (text: string, scenario: ScenarioInput | null, clipId: string | null): Promise<{ run_id: string }> =>
  postJson("/agent/run", { headers: json, body: JSON.stringify({ text, scenario, clip_id: clipId }) });

export function startAgentVoiceRun(wav: Blob): Promise<{ run_id: string; transcript: string }> {
  const form = new FormData();
  form.append("audio", wav, "question.wav");
  return postJson("/agent/run-voice", { body: form });
}

export const fetchTrace = (runId: string): Promise<TraceSnapshot> => getJson(`/agent/trace/${encodeURIComponent(runId)}`);
export const artifactUrl = (runId: string, name: string): string => apiUrl(`/agent/trace/${encodeURIComponent(runId)}/artifact/${encodeURIComponent(name)}`);

/** Follows a run live (Server-Sent Events). Returns a function that closes the stream. */
export function followRun(runId: string, onEvent: (event: TraceEvent) => void, onEnd: () => void): () => void {
  const source = new EventSource(apiUrl(`/agent/trace/${encodeURIComponent(runId)}/stream`));
  source.onmessage = (message) => {
    const event = JSON.parse(message.data) as TraceEvent;
    onEvent(event);
    if (event.kind === "run_end") {
      source.close();
      onEnd();
    }
  };
  source.onerror = () => {
    source.close();
    onEnd();
  };
  return () => source.close();
}

export const startDemo = (mode: "offline" | "live", autoConfirm: boolean): Promise<{ run_id: string; mode: string }> =>
  postJson("/demo/run", { headers: json, body: JSON.stringify({ mode, auto_confirm: autoConfirm }) });
export const recordedCardUrl = (): string => apiUrl("/demo/card");

export const fetchProposals = (): Promise<{ proposals: ProposalInfo[] }> => getJson("/agent/proposals");
export const approveProposal = (id: string, operatorConfirmationId: string): Promise<{ proposal: ProposalInfo }> =>
  postJson(`/agent/proposals/${encodeURIComponent(id)}/approve`, { headers: json, body: JSON.stringify({ operator_confirmation_id: operatorConfirmationId }) });
export const rejectProposal = (id: string): Promise<{ proposal: ProposalInfo }> =>
  postJson(`/agent/proposals/${encodeURIComponent(id)}/reject`, { headers: json, body: "{}" });

export const analyseDemoClip = (): Promise<WatchResult> => postJson("/watch/demo-clip", { headers: json, body: "{}" });
export function analyseClip(file: File, markerCm: number, registerClip: boolean): Promise<WatchResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("marker_height_cm", String(markerCm));
  form.append("no_people_confirmed", "true");
  form.append("register_clip", String(registerClip));
  return postJson("/watch/clip", { body: form });
}

export const fetchBacktest = (): Promise<BacktestResponse> => getJson("/backtest");
export const fetchLearnedModel = (): Promise<LearnedModelInfo> => getJson("/learned-model");
export const fetchGaugeValidation = (): Promise<GaugeValidation> => getJson("/validation/gauge");
export const fetchEnfen = (): Promise<{ source: string; bulletins: EnfenBulletin[] }> => getJson("/enfen/timeline");

export interface CardReply {
  tier: string;
  image_png_base64: string;
  intended_text: Record<string, string>;
  review_required: boolean;
}
export const generateCard = (
  settlementName: string,
  distanceKm: number,
  settlementId: string | null,
  tier: string,
): Promise<CardReply> =>
  postJson("/cards/evacuation", {
    headers: json,
    body: JSON.stringify({ settlement_name: settlementName, distance_km: distanceKm, settlement_id: settlementId, tier, synthetic_household: true }),
  });

// --- public signals ---------------------------------------------------------------------------------------------------
export const fetchSignals = (): Promise<SignalsSnapshot> => getJson<SignalsSnapshot>("/signals");
export const setSimulatedSignals = (enabled: boolean): Promise<SignalsSnapshot> =>
  postJson("/signals/simulated", { headers: json, body: JSON.stringify({ enabled }) });
export const refreshSignals = (): Promise<{ report: NonNullable<SignalsSnapshot["last_refresh"]>; signals: SignalsSnapshot }> =>
  postJson("/signals/refresh", { headers: json, body: "{}" });
