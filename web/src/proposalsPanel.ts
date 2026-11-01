import { approveProposal, fetchProposals, rejectProposal } from "./consoleApi";
import type { ProposalInfo } from "./consoleTypes";
import { el } from "./panel";

const STATUS_ES: Record<ProposalInfo["status"], string> = {
  awaiting_operator: "Esperando al operador",
  approved: "Aprobada",
  rejected: "Rechazada",
  dispatched: "Enviada",
};

interface ProposalsOptions {
  /** Called after the operator approves or rejects, so households, cases and the globe refresh. */
  onChanged(): void;
  /** Number of proposals waiting for a click (tab badge). */
  onPending(count: number): void;
}

export interface ProposalsPanel {
  refresh(): Promise<void>;
}

export function initProposals(options: ProposalsOptions): ProposalsPanel {
  const list = document.getElementById("proposals") as HTMLElement;
  const drafts = new Map<string, string>(); // confirmation ids typed so far survive a refresh
  let signature = ""; // the list is rebuilt only when a proposal changed, so typing is never interrupted

  const card = (p: ProposalInfo): HTMLElement => {
    const item = el("li", `proposal tier-${p.tier.toLowerCase()} status-${p.status}`);
    const head = el("div", "proposal-head");
    head.append(
      el("span", `tier-pill tier-${p.tier.toLowerCase()}`, p.tier),
      el("strong", undefined, p.site_name),
      el("span", "chip", STATUS_ES[p.status]),
    );
    item.append(head);
    const facts = [
      `${p.households_targeted} hogares sintéticos aguas abajo`,
      p.evaluated_tier !== p.tier ? `las reglas devuelven ${p.evaluated_tier} hasta que usted confirme` : `las reglas devuelven ${p.evaluated_tier}`,
    ];
    if (p.water_rise_cm !== null) facts.push(`subida medida: ${p.water_rise_cm.toFixed(1)} cm`);
    item.append(el("p", "muted", facts.join(" · ")));
    if (p.reasons.length) {
      const reasons = el("ul", "reasons");
      for (const reason of p.reasons.slice(0, 5)) reasons.append(el("li", undefined, reason));
      item.append(reasons);
    }
    if (p.status !== "awaiting_operator") {
      if (p.operator_confirmation_id) item.append(el("p", "muted", `Confirmada por ${p.operator_confirmation_id}.`));
      return item;
    }

    const field = el("input");
    field.type = "text";
    field.maxLength = 80;
    field.placeholder = "Su id de operador, p. ej. OP-2026-0007";
    field.setAttribute("aria-label", "Id de confirmación del operador");
    field.value = drafts.get(p.id) ?? "";
    const confirm = el("button", "danger", p.tier === "EVACUATION" ? "Confirmar evacuación y enviar" : "Confirmar y enviar");
    confirm.type = "button";
    confirm.disabled = field.value.trim() === "";
    const reject = el("button", undefined, "Rechazar");
    reject.type = "button";
    const note = el("p", "muted legend", "Nada se envía hasta este clic. El nivel se vuelve a evaluar con las reglas al confirmar.");
    const status = el("p", "muted", "");
    field.addEventListener("input", () => {
      drafts.set(p.id, field.value);
      confirm.disabled = field.value.trim() === "";
    });
    confirm.addEventListener("click", () => {
      confirm.disabled = true;
      status.textContent = "Enviando…";
      approveProposal(p.id, field.value.trim())
        .then(() => {
          drafts.delete(p.id);
          options.onChanged();
          return refresh();
        })
        .catch((error: Error) => {
          status.textContent = `No se envió: ${error.message}`;
          confirm.disabled = field.value.trim() === "";
        });
    });
    reject.addEventListener("click", () => {
      reject.disabled = true;
      rejectProposal(p.id)
        .then(() => refresh())
        .catch((error: Error) => {
          status.textContent = `No se pudo rechazar: ${error.message}`;
        });
    });
    const buttons = el("div", "row-buttons");
    buttons.append(confirm, reject);
    item.append(field, buttons, note, status);
    return item;
  };

  async function refresh(): Promise<void> {
    try {
      const { proposals } = await fetchProposals();
      const next = JSON.stringify(proposals.map((p) => [p.id, p.status, p.evaluated_tier, p.water_rise_cm]));
      options.onPending(proposals.filter((p) => p.status === "awaiting_operator").length);
      if (next === signature || list.querySelector("input:focus")) return;
      signature = next;
      list.replaceChildren(...(proposals.length ? [...proposals].reverse().map(card) : [el("li", "muted", "Sin propuestas. Los agentes proponen; usted decide.")]));
    } catch {
      // the backend may be restarting; the next event refreshes again
    }
  }

  void refresh();
  return { refresh };
}
