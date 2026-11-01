// Types of the orchestrator console (agent runs, traces, proposals, demo, evidence).
import type { UiAction } from "./types";

export interface TraceEvent {
  seq: number;
  ts: string;
  run_id: string;
  kind: string; // run_start | agent_start | agent_end | model_call | tool_call | guardrail | step | proposal | operator_action | run_end
  agent: string;
  model: string;
  tool: string;
  args_summary: string;
  result_summary: string;
  status: "ok" | "error";
  latency_ms: number | null;
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  detail: Record<string, unknown>;
}

export interface TraceTotals {
  cost_usd: number;
  model_calls: number;
  tool_calls: number;
  models: string[];
  agents: string[];
}

export interface RunResult {
  answer_es: string;
  ui_actions: UiAction[];
  tools_used: string[];
  flags: string[];
  proposals: ProposalInfo[];
  facts: Record<string, number>;
  artifacts_info: Record<string, unknown>;
  error?: string;
  outcome?: string;
}

export interface TraceSnapshot {
  run_id: string;
  status: "running" | "completed" | "failed";
  events: TraceEvent[];
  totals: TraceTotals;
  result: RunResult | null;
  artifacts: string[];
}

export interface ProposalInfo {
  id: string;
  run_id: string;
  site_id: string;
  site_name: string;
  tier: string;
  evaluated_tier: string;
  reasons: string[];
  households_targeted: number;
  status: "awaiting_operator" | "approved" | "rejected" | "dispatched";
  requires_operator_confirmation_id: boolean;
  operator_confirmation_id: string | null;
  water_rise_cm: number | null;
  dispatch_id: string | null;
}

export interface AgentConfig {
  live: boolean;
  planner_model: string;
  planner_fallback_model: string;
  specialist_model: string;
  run_budget_usd: number;
  budget: { spent_usd: number; limit_usd: number } | null;
  calls_mode: string;
}

export interface DemoStatus {
  running: boolean;
  run_id: string | null;
  mode: string;
  offline_available: boolean;
  live_available: boolean;
  calls_mode: string;
}

export interface Health {
  status: string;
  synthetic_data: boolean;
  data_mode: string;
  calls_mode: string;
}

export interface ScenarioInput {
  enfen_magnitude?: string;
  senamhi_level?: string;
  forecast_rain_72h_mm?: number;
  observed_rain_24h_mm?: number;
  water_rise_cm?: number;
}

export interface WatchResult {
  rise_cm: number | null;
  confidence: number;
  valid_frames: number;
  total_frames: number;
  reasons: string[];
  analyzer?: string;
  clip_id?: string;
  synthetic_clip?: boolean;
  billed_frames: number | null;
  estimated_cost_usd: number | null;
  series: { frame: number; valid: boolean; rise_cm: number | null }[];
  overlays: { frame: number; png_base64: string }[];
  tier_input: { water_rise_cm: number | null };
  note: string;
}

export interface Ci {
  auc: number | null;
  auc_ci95: [number, number] | null;
}

export interface BacktestEvent {
  event: string;
  n_districts: number;
  n_positive: number;
  base_rate: number;
  models: Record<string, Ci>;
}

export interface BacktestResponse {
  events: Record<string, BacktestEvent>;
}

export interface LearnedModelInfo {
  split: { train_seasons: number[]; test_seasons: number[] };
  model: { coefficients: Record<string, number>; train_base_rate: number; l2: number };
  evaluation: Record<
    string,
    {
      n_positive: number;
      base_rate: number;
      learned: { auc: number; auc_ci95: [number, number]; brier: number; mean_predicted: number };
      ensemble_learned_cenepred: Ci;
      reference: Record<string, Ci>;
    }
  >;
  caveat: string;
}

export interface EnfenBulletin {
  date: string;
  number: string;
  alert_status: string;
  magnitude: string;
}

// --- public signals (unverified news / web reports, context only) ----------------------------------------------------

export interface SignalSource {
  url: string;
  domain: string;
  event_type: string;
  event_time: string;
}

export interface SignalDistrict {
  ubigeo: string;
  district: string;
  count: number;
  latest: string;
  lon: number;
  lat: number;
  label: string;
  simulated: boolean;
  sources: SignalSource[];
}

export interface SignalsSnapshot {
  mode: "live" | "simulated" | "off";
  enabled: boolean;
  simulated: boolean;
  simulation_available: boolean;
  label: string;
  disclaimer: string;
  window_hours: number;
  min_corroborating_signals: number;
  refreshed_at: string | null;
  note: string;
  last_refresh: { queries: number; results_seen: number; events_extracted: number; signals_kept: number; dropped_unmatched: number; errors: string[] } | null;
  districts: SignalDistrict[];
}
