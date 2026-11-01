import type { Viewer } from "cesium";
import { fetchSignals, refreshSignals, setSimulatedSignals } from "./consoleApi";
import type { SignalDistrict, SignalSource, SignalsSnapshot } from "./consoleTypes";
import { setSignalsVisible, showSignals } from "./layers";
import { el } from "./panel";

const EVENT_LABELS: Record<string, string> = {
  huaico: "huaico",
  deslizamiento: "deslizamiento",
  desborde: "desborde",
  relave: "relave",
  inundacion: "inundación",
};

export interface SignalsPanel {
  refresh(): Promise<void>;
  /** Demo mode: show the labelled simulated set when no live signals exist. */
  ensureSimulated(): Promise<void>;
}

interface Options {
  viewer: Viewer;
  flyTo(lon: number, lat: number): void;
}

/** Only plain web links are ever rendered as links. */
function safeHref(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function ago(iso: string, now = Date.now()): string {
  const hours = Math.max(0, (now - Date.parse(iso)) / 3_600_000);
  if (Number.isNaN(hours)) return "";
  if (hours < 1) return "hace menos de 1 h";
  return `hace ${Math.round(hours)} h`;
}

function sourceRow(source: SignalSource, simulated: boolean): HTMLElement {
  const row = el("li", "signal-source");
  const href = safeHref(source.url);
  if (href) {
    const link = el("a", undefined, source.domain);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer nofollow";
    link.title = source.url;
    row.append(link);
  } else {
    row.append(el("span", undefined, source.domain));
  }
  row.append(el("span", "muted", ` · ${EVENT_LABELS[source.event_type] ?? source.event_type} · ${ago(source.event_time)}`));
  if (simulated) row.append(el("span", "badge badge-sim", "simulado"));
  return row;
}

function districtItem(district: SignalDistrict, onFocus: (d: SignalDistrict) => void): HTMLElement {
  const item = el("li", "signal-item");
  const head = el("button", "signal-head");
  head.type = "button";
  head.title = "Centrar el mapa en este distrito";
  head.append(
    el("span", "signal-name", district.district || "Sin distrito"),
    el("span", "signal-count", `${district.count} ${district.count === 1 ? "página" : "páginas"}`),
  );
  if (district.simulated) head.append(el("span", "badge badge-sim", "simulado"));
  head.addEventListener("click", () => onFocus(district));
  item.append(head, el("p", "muted legend", `Último reporte ${ago(district.latest)}`));
  const list = el("ul", "signal-sources");
  for (const source of district.sources) list.append(sourceRow(source, district.simulated));
  item.append(list);
  return item;
}

export function initSignals(options: Options): SignalsPanel {
  const status = document.getElementById("signals-status") as HTMLElement;
  const banner = document.getElementById("signals-banner") as HTMLElement;
  const list = document.getElementById("signals-list") as HTMLElement;
  const simulate = document.getElementById("signals-sim") as HTMLButtonElement;
  const refreshButton = document.getElementById("signals-refresh") as HTMLButtonElement;
  const toggle = document.getElementById("toggle-signals") as HTMLInputElement;
  let current: SignalsSnapshot | null = null;

  const focus = (d: SignalDistrict): void => options.flyTo(d.lon, d.lat);

  const render = (snapshot: SignalsSnapshot): void => {
    current = snapshot;
    showSignals(options.viewer, snapshot.districts, toggle.checked);
    banner.textContent = snapshot.disclaimer;
    const modeText =
      snapshot.mode === "live"
        ? `En vivo (búsqueda en páginas web públicas, últimas ${snapshot.window_hours} h)`
        : snapshot.mode === "simulated"
          ? "Conjunto SIMULADO para la demostración: los reportes y enlaces son inventados"
          : "Apagadas (WILLAY_SIGNALS_ENABLED=0): no se buscó nada";
    status.textContent = `${modeText}.${snapshot.refreshed_at ? ` Última actualización ${ago(snapshot.refreshed_at)}.` : ""}${snapshot.note && snapshot.mode === "off" ? ` ${snapshot.note}.` : ""}`;
    simulate.hidden = snapshot.mode === "live" || !snapshot.simulation_available;
    simulate.textContent = snapshot.mode === "simulated" ? "Ocultar señales simuladas" : "Mostrar señales simuladas (demostración)";
    refreshButton.hidden = snapshot.mode !== "live";
    if (snapshot.districts.length === 0) {
      list.replaceChildren(
        el(
          "li",
          "muted",
          snapshot.mode === "live"
            ? "Sin reportes ubicados en las últimas 72 horas."
            : snapshot.mode === "simulated"
              ? "El conjunto simulado no tiene reportes dentro de la ventana."
              : "No hay señales que mostrar.",
        ),
      );
      return;
    }
    list.replaceChildren(...snapshot.districts.map((d) => districtItem(d, focus)));
  };

  const refresh = async (): Promise<void> => {
    try {
      render(await fetchSignals());
    } catch (error) {
      status.textContent = `No se pudieron cargar las señales públicas: ${(error as Error).message}`;
    }
  };

  simulate.addEventListener("click", () => {
    simulate.disabled = true;
    setSimulatedSignals(current?.mode !== "simulated")
      .then(render)
      .catch((error: Error) => (status.textContent = error.message))
      .finally(() => (simulate.disabled = false));
  });

  refreshButton.addEventListener("click", () => {
    refreshButton.disabled = true;
    status.textContent = "Buscando en la web y leyendo con Muse Spark…";
    refreshSignals()
      .then((reply) => {
        render(reply.signals);
        const r = reply.report;
        status.textContent += ` Se revisaron ${r.results_seen} páginas, se ubicaron ${r.signals_kept} reportes y se descartaron ${r.dropped_unmatched} por lugar no reconocido.${r.errors.length ? ` Avisos: ${r.errors.join("; ")}.` : ""}`;
      })
      .catch((error: Error) => (status.textContent = error.message))
      .finally(() => (refreshButton.disabled = false));
  });

  toggle.addEventListener("change", () => setSignalsVisible(options.viewer, toggle.checked));

  return {
    refresh,
    async ensureSimulated() {
      await refresh();
      if (current && current.mode === "off" && current.simulation_available) {
        try {
          render(await setSimulatedSignals(true));
        } catch {
          // the demo still runs without the signals layer
        }
      }
    },
  };
}
