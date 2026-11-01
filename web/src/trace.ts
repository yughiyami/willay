import type { TraceEvent } from "./consoleTypes";
import { el } from "./panel";

export const AGENT_LABELS: Record<string, string> = {
  planner: "Planificador",
  hazard_analyst: "Analista de peligro",
  watcher: "Vigía",
  dispatcher: "Despachador",
  communicator: "Comunicador",
  demo: "Demostración",
  operator: "Operador",
};

interface ModelStyle {
  css: string;
  label: string;
}

/** The badge a model gets in the timeline: what judges look at to see which Meta model did what. */
export function modelStyle(model: string): ModelStyle {
  if (model.startsWith("muse-wonder")) return { css: "m-wonder", label: "muse-wonder" };
  if (model.startsWith("muse-spark")) return { css: "m-spark", label: "muse-spark" };
  if (model.startsWith("sam-3.1")) return { css: "m-sam", label: "sam-3.1" };
  if (model.startsWith("muse-image")) return { css: "m-image", label: "muse-image" };
  if (model.startsWith("muse-voice")) return { css: "m-voice", label: "voice-transcribe" };
  return { css: "m-other", label: model || "modelo" };
}

function money(value: number | null): string {
  return value === null || value === undefined ? "" : value === 0 ? "$0" : `$${value.toFixed(4)}`;
}

function modelBadge(event: TraceEvent): HTMLElement {
  const style = modelStyle(event.model);
  const badge = el("span", `badge ${style.css}${event.status === "error" ? " badge-error" : ""}`, style.label);
  badge.title = event.model;
  return badge;
}

function chip(text: string, className = "chip"): HTMLElement {
  return el("span", className, text);
}

export function renderEvent(event: TraceEvent): HTMLElement | null {
  const agent = AGENT_LABELS[event.agent] ?? event.agent;
  const row = el("li", `ev ev-${event.kind} agent-${event.agent || "none"}${event.status === "error" ? " ev-error" : ""}`);
  const head = el("div", "ev-head");
  switch (event.kind) {
    case "step": {
      row.className = "ev ev-step";
      head.append(el("strong", undefined, String(event.detail["title"] ?? event.result_summary)));
      const note = event.detail["note"];
      row.append(head);
      if (typeof note === "string") row.append(el("p", "ev-note", note));
      return row;
    }
    case "run_start":
      return null;
    case "run_end":
      row.className = "ev ev-end";
      head.append(el("span", undefined, event.result_summary === "completed" ? "Corrida terminada" : "Corrida fallida"));
      row.append(head);
      return row;
    case "agent_start":
      row.className = `ev ev-agent agent-${event.agent}`;
      head.append(chip(agent, "chip chip-agent"), el("span", "ev-text", "recibe la tarea"));
      if (event.model) head.append(modelBadge(event));
      row.append(head);
      return row;
    case "agent_end": {
      const said = event.detail["text"];
      if (typeof said !== "string" || !said) return null;
      row.className = `ev ev-say agent-${event.agent}`;
      head.append(chip(agent, "chip chip-agent"), el("span", "ev-text", "responde"));
      row.append(head, el("blockquote", "ev-quote", said.length > 420 ? `${said.slice(0, 420)}…` : said));
      return row;
    }
    case "model_call": {
      head.append(chip(agent, "chip chip-agent"), modelBadge(event), el("span", "ev-text", event.status === "error" ? "falló" : event.result_summary));
      if (event.detail["replayed"]) head.append(chip("grabado", "chip chip-replay"));
      if (event.detail["standin"]) head.append(chip("sustituto sin red", "chip chip-replay"));
      const meta = el("div", "ev-meta");
      const parts = [
        event.latency_ms !== null ? `${event.latency_ms} ms` : "",
        event.input_tokens ? `${event.input_tokens} → ${event.output_tokens} tok` : "",
        money(event.cost_usd),
      ].filter(Boolean);
      meta.textContent = parts.join(" · ");
      row.append(head, meta);
      if (event.status === "error") row.append(el("p", "ev-note", event.result_summary));
      return row;
    }
    case "tool_call": {
      head.append(chip(agent, "chip chip-agent"), el("code", "ev-tool", event.tool));
      const isDelegation = event.tool.startsWith("ask_");
      if (isDelegation) head.append(chip("delega", "chip chip-delegate"));
      row.append(head);
      const detail = el("div", "ev-meta");
      detail.textContent = [event.args_summary, event.result_summary ? `→ ${event.result_summary}` : "", event.latency_ms !== null ? `${event.latency_ms} ms` : ""]
        .filter(Boolean)
        .join("  ");
      row.append(detail);
      return row;
    }
    case "guardrail":
      row.className = "ev ev-guard";
      head.append(chip("Salvaguarda", "chip chip-guard"), el("span", "ev-text", event.result_summary));
      row.append(head);
      return row;
    case "proposal":
      row.className = "ev ev-proposal";
      head.append(chip("Propuesta", "chip chip-proposal"), el("span", "ev-text", event.result_summary));
      row.append(head);
      return row;
    case "operator_action":
      row.className = "ev ev-operator";
      head.append(chip("Operador", "chip chip-operator"), el("span", "ev-text", event.result_summary));
      row.append(head);
      return row;
    default:
      return null;
  }
}

/** Live timeline of a run plus a running total of calls and estimated cost. */
export class TraceView {
  private events: TraceEvent[] = [];

  constructor(
    private readonly list: HTMLElement,
    private readonly totals: HTMLElement,
  ) {}

  clear(): void {
    this.events = [];
    this.list.replaceChildren();
    this.totals.replaceChildren();
  }

  add(event: TraceEvent): void {
    if (this.events.some((e) => e.seq === event.seq && e.run_id === event.run_id)) return;
    this.events.push(event);
    const node = renderEvent(event);
    if (node) {
      this.list.append(node);
      this.list.scrollTop = this.list.scrollHeight;
    }
    this.renderTotals();
  }

  private renderTotals(): void {
    const models = new Map<string, number>();
    let cost = 0;
    let tools = 0;
    for (const e of this.events) {
      cost += e.cost_usd ?? 0;
      if (e.kind === "tool_call") tools += 1;
      if (e.kind === "model_call" && e.model) models.set(e.model, (models.get(e.model) ?? 0) + 1);
    }
    const children: HTMLElement[] = [];
    for (const [model, count] of models) {
      const style = modelStyle(model);
      const badge = el("span", `badge ${style.css}`, `${style.label} ×${count}`);
      badge.title = model;
      children.push(badge);
    }
    children.push(el("span", "chip", `${tools} herramientas`), el("span", "chip", `costo estimado ${money(cost)}`));
    this.totals.replaceChildren(...children);
  }
}
