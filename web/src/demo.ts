import type { AgentConsole } from "./agentConsole";
import { startDemo } from "./consoleApi";
import type { TraceEvent } from "./consoleTypes";
import { el } from "./panel";

const STEPS: { key: string; label: string }[] = [
  { key: "enfen", label: "ENFEN extraordinario" },
  { key: "senamhi", label: "SENAMHI naranja" },
  { key: "watcher", label: "Vigía: el río sube" },
  { key: "operator", label: "Operador confirma" },
  { key: "dispatch", label: "Envío a hogares" },
  { key: "household_reply", label: "Hogar responde 2" },
  { key: "done", label: "Caso abierto" },
];

interface DemoOptions {
  agents: AgentConsole;
  flyToSite(siteId: string): void;
  selectTab(tab: string): void;
  refreshProposals(): void;
  refreshAlerts(): void;
  refreshChips(): void;
}

export interface Demo {
  handleEvent(event: TraceEvent): void;
}

/** The "Modo demostración" button: runs the scripted scenario (recorded or live) and follows it on the console. */
export function initDemo(options: DemoOptions): Demo {
  const button = document.getElementById("demo-run") as HTMLButtonElement;
  const mode = document.getElementById("demo-mode") as HTMLSelectElement;
  const auto = document.getElementById("demo-auto") as HTMLInputElement;
  const bar = document.getElementById("demo-bar") as HTMLElement;
  const list = document.getElementById("demo-steps") as HTMLOListElement;
  const message = document.getElementById("demo-msg") as HTMLElement;
  let active = false;

  const renderSteps = (current: number): void => {
    list.replaceChildren(
      ...STEPS.map((step, index) => {
        const state = index < current ? "done" : index === current ? "current" : "todo";
        const item = el("li", `demo-step ${state}`);
        item.append(el("span", "demo-n", String(index + 1)), el("span", undefined, step.label));
        if (state === "current") item.setAttribute("aria-current", "step");
        return item;
      }),
    );
  };

  const setMessage = (text: string, attention = false): void => {
    message.textContent = text;
    message.className = attention ? "demo-attention" : "";
  };

  button.addEventListener("click", () => {
    if (active) return;
    active = true;
    button.disabled = true;
    bar.hidden = false;
    renderSteps(-1);
    setMessage("Iniciando la demostración…");
    const selected = mode.value as "offline" | "live";
    startDemo(selected, auto.checked)
      .then(async (run) => {
        options.selectTab("agentes");
        const origin = selected === "live" ? "con los modelos de Meta en vivo" : "con turnos de modelo grabados (sin red, sin costo)";
        await options.agents.followExternalRun(run.run_id, `Modo demostración ${origin}. Se ejecutan el orquestador, las reglas y las salvaguardas reales.`);
      })
      .catch((error: Error) => {
        setMessage(`No se pudo iniciar: ${error.message}`, true);
      })
      .finally(() => {
        active = false;
        button.disabled = false;
        options.refreshChips();
        options.refreshAlerts();
        options.refreshProposals();
      });
  });

  return {
    handleEvent(event) {
      if (event.kind === "proposal") options.refreshProposals();
      if (event.kind === "operator_action") {
        options.refreshProposals();
        options.refreshAlerts();
        if (active) {
          setMessage(event.result_summary);
          options.selectTab("agentes");
        }
      }
      if (event.kind === "run_end" && active) setMessage(event.status === "ok" ? "Demostración terminada." : "La demostración se detuvo.");
      if (event.kind !== "step") return;
      const key = String(event.detail["key"] ?? "");
      if (key === "start") {
        const siteId = String(event.detail["site_id"] ?? "");
        if (siteId) options.flyToSite(siteId);
        setMessage(`${event.result_summary}. ${String(event.detail["note"] ?? "")}`);
        return;
      }
      const index = STEPS.findIndex((s) => s.key === key);
      if (index >= 0) renderSteps(key === "done" ? STEPS.length : index);
      if (key === "operator") {
        options.selectTab("alertas");
        options.refreshProposals();
        setMessage(
          auto.checked
            ? "El clic del operador se simula (casilla «confirmar solo»); queda escrito como simulado en el registro."
            : "Los agentes dejaron una propuesta de EVACUACIÓN pendiente. Escriba su id de operador y pulse «Confirmar evacuación y enviar» en Alertas.",
          !auto.checked,
        );
      } else if (key === "dispatch" || key === "household_reply") {
        options.refreshAlerts();
        setMessage(event.result_summary);
      } else if (key === "done") {
        options.refreshAlerts();
        setMessage("Fin: se envió a hogares sintéticos, uno respondió 2 y hay un caso abierto para el operador.");
      } else {
        setMessage(event.result_summary);
      }
    },
  };
}
