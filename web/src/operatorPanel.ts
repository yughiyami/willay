import { dispatchAlerts, fetchCases, fetchHouseholdAlerts, sendSimulatorReply } from "./api";
import { el } from "./panel";
import type { HouseholdAlert, HouseholdResponse, HouseholdTier, OperatorCase, Site } from "./types";

interface OperatorPanelOptions {
  /** Sites to alert (ranked list). */
  sites: Site[];
  /** Called after every refresh so the globe can highlight households that need a human. */
  onAlerts(alerts: HouseholdAlert[]): void;
  /** Called when the operator picks a case or household, to fly the camera there. */
  onFocus(lon: number, lat: number): void;
}

export interface OperatorPanel {
  setSite(siteId: string, evaluatedTier: string | null): void;
  refresh(): Promise<void>;
}

const STATUS_LABELS: Record<string, string> = {
  AWAITING: "Enviado, sin respuesta aún",
  SAFE: "Respondió: a salvo",
  NEEDS_HELP: "Respondió: NECESITA AYUDA",
  NO_ANSWER: "SIN RESPUESTA",
};

export function statusOf(alert: HouseholdAlert): keyof typeof STATUS_LABELS {
  return alert.response ?? "AWAITING";
}

/** Operator panel: send alerts to the synthetic households, follow their answers, and try the phone simulator. */
export function initOperatorPanel(options: OperatorPanelOptions): OperatorPanel {
  const siteSelect = document.getElementById("op-site") as HTMLSelectElement;
  const tierSelect = document.getElementById("op-tier") as HTMLSelectElement;
  const confirmRow = document.getElementById("op-confirm-row") as HTMLElement;
  const confirmInput = document.getElementById("op-confirm") as HTMLInputElement;
  const evaluatedEl = document.getElementById("op-evaluated") as HTMLElement;
  const sendButton = document.getElementById("op-send") as HTMLButtonElement;
  const refreshButton = document.getElementById("op-refresh") as HTMLButtonElement;
  const statusEl = document.getElementById("op-status") as HTMLElement;
  const countsEl = document.getElementById("op-counts") as HTMLElement;
  const tableBody = document.getElementById("op-households") as HTMLElement;
  const casesEl = document.getElementById("op-cases") as HTMLElement;
  const phoneEl = document.getElementById("phone-screen") as HTMLElement;
  const phoneTitle = document.getElementById("phone-title") as HTMLElement;
  const phoneButtons = document.getElementById("phone-buttons") as HTMLElement;

  let alerts: HouseholdAlert[] = [];
  let selectedAlertId: string | null = null;

  for (const site of options.sites) {
    const option = el("option", undefined, `#${site.rank} ${site.name}`);
    option.value = site.id;
    siteSelect.append(option);
  }

  const updateConfirmVisibility = (): void => {
    confirmRow.hidden = tierSelect.value !== "EVACUATION";
    sendButton.disabled = tierSelect.value === "EVACUATION" && confirmInput.value.trim() === "";
  };
  tierSelect.addEventListener("change", updateConfirmVisibility);
  confirmInput.addEventListener("input", updateConfirmVisibility);
  updateConfirmVisibility();

  const renderPhone = (): void => {
    const alert = alerts.find((a) => a.id === selectedAlertId) ?? null;
    phoneButtons.replaceChildren();
    if (!alert) {
      phoneTitle.textContent = "Seleccione un hogar";
      phoneEl.replaceChildren(el("p", "phone-empty", "Elija un hogar de la lista para leer su mensaje."));
      return;
    }
    phoneTitle.textContent = alert.household_label;
    const bubble = el("div", "bubble in");
    for (const line of alert.message_text.split("\n")) bubble.append(el("p", undefined, line));
    const meta = el("span", "bubble-meta", `Willay Relaves - ${alert.message_status === "sent" ? "entregado (simulado)" : alert.message_status}`);
    bubble.append(meta);
    const children: HTMLElement[] = [bubble];
    if (alert.response_channel === "whatsapp" && alert.response) {
      children.push(el("div", "bubble out", alert.response === "SAFE" ? "1 - Estoy a salvo" : alert.response === "NEEDS_HELP" ? "2 - Necesito ayuda" : "(sin respuesta)"));
    }
    if (alert.call_status !== "not_attempted") {
      children.push(el("p", "phone-call", `Llamada: ${alert.call_status}${alert.call_detail ? ` (${alert.call_detail})` : ""}`));
    }
    phoneEl.replaceChildren(...children);
    const reply = (response: HouseholdResponse, label: string): HTMLButtonElement => {
      const button = el("button", "phone-key", label);
      button.type = "button";
      button.addEventListener("click", () => void answer(alert.id, response));
      return button;
    };
    phoneButtons.append(reply("SAFE", "1  Estoy a salvo"), reply("NEEDS_HELP", "2  Necesito ayuda"));
  };

  const renderCases = (cases: OperatorCase[]): void => {
    const open = cases.filter((c) => c.status === "open");
    casesEl.replaceChildren(
      ...(open.length === 0
        ? [el("li", "muted", "Sin casos abiertos.")]
        : open.map((c) => {
            const item = el("li");
            const button = el("button", `case-row ${c.reason === "NEEDS_HELP" ? "help" : "silent"}`);
            button.type = "button";
            button.append(
              el("strong", undefined, c.reason === "NEEDS_HELP" ? "PIDE AYUDA" : "SIN RESPUESTA"),
              el("span", undefined, ` ${c.household_label} - ${c.settlement_name}`),
            );
            button.title = c.note;
            button.addEventListener("click", () => {
              selectedAlertId = c.alert_id;
              renderPhone();
              if (c.lon !== null && c.lat !== null) options.onFocus(c.lon, c.lat);
            });
            item.append(button);
            return item;
          })),
    );
  };

  const renderTable = (): void => {
    tableBody.replaceChildren(
      ...alerts.map((alert) => {
        const status = statusOf(alert);
        const row = el("tr", `hh-row hh-${status.toLowerCase()}${alert.id === selectedAlertId ? " selected" : ""}`);
        row.tabIndex = 0;
        row.append(
          el("td", undefined, alert.household_label),
          el("td", undefined, alert.settlement_name),
          el("td", "hh-status", STATUS_LABELS[status]),
        );
        const select = (): void => {
          selectedAlertId = alert.id;
          renderTable();
          renderPhone();
          options.onFocus(alert.lon, alert.lat);
        };
        row.addEventListener("click", select);
        row.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") select();
        });
        return row;
      }),
    );
    if (alerts.length === 0) {
      const row = el("tr");
      const cell = el("td", "muted", "Aún no se enviaron alertas.");
      cell.colSpan = 3;
      row.append(cell);
      tableBody.append(row);
    }
  };

  const refresh = async (): Promise<void> => {
    try {
      const [households, cases] = await Promise.all([fetchHouseholdAlerts(), fetchCases()]);
      alerts = households.alerts;
      const c = households.counts;
      countsEl.textContent = `Sin respuesta aún ${c.AWAITING} · A salvo ${c.SAFE} · Piden ayuda ${c.NEEDS_HELP} · Sin respuesta ${c.NO_ANSWER}`;
      renderTable();
      renderPhone();
      renderCases(cases.cases);
      options.onAlerts(alerts);
    } catch (error) {
      statusEl.textContent = `No se pudieron cargar las alertas: ${(error as Error).message}`;
    }
  };

  const answer = async (alertId: string, response: HouseholdResponse): Promise<void> => {
    try {
      await sendSimulatorReply(alertId, response);
      statusEl.textContent = "Respuesta registrada.";
      await refresh();
    } catch (error) {
      statusEl.textContent = `No se pudo registrar la respuesta: ${(error as Error).message}`;
    }
  };

  sendButton.addEventListener("click", () => {
    const tier = tierSelect.value as HouseholdTier;
    const confirmation = confirmInput.value.trim() || null;
    sendButton.disabled = true;
    statusEl.textContent = "Enviando…";
    dispatchAlerts(siteSelect.value, tier, tier === "EVACUATION" ? confirmation : null)
      .then(async (reply) => {
        statusEl.textContent = `${reply.tier} enviado a ${reply.households_targeted} hogar(es) sintético(s). Llamadas: ${reply.calls_mode}.`;
        selectedAlertId = reply.alerts[0]?.id ?? selectedAlertId;
        await refresh();
      })
      .catch((error) => {
        statusEl.textContent = `No se envió: ${(error as Error).message}`;
      })
      .finally(updateConfirmVisibility);
  });
  refreshButton.addEventListener("click", () => void refresh());

  void refresh();

  return {
    refresh,
    setSite(siteId, evaluatedTier) {
      if ([...siteSelect.options].some((o) => o.value === siteId)) siteSelect.value = siteId;
      evaluatedEl.textContent = evaluatedTier ? `Nivel que devuelven las reglas ahora: ${evaluatedTier}` : "";
    },
  };
}
