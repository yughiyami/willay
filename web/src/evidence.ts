import { fetchBacktest, fetchEnfen, fetchGaugeValidation, fetchLearnedModel } from "./consoleApi";
import type { BacktestEvent, Ci, EnfenBulletin, LearnedModelInfo } from "./consoleTypes";
import { dataQualityLabel } from "./labels";
import { el } from "./panel";
import type { Site } from "./types";

const NS = "http://www.w3.org/2000/svg";

function svg(tag: string, attrs: Record<string, string | number>, text?: string): SVGElement {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

const MODEL_LABELS: Record<string, string> = {
  willay_static: "Puntaje estático (propio)",
  cenepred_baseline: "CENEPRED (media)",
  ensemble_static_cenepred: "Conjunto estático + CENEPRED",
  population_baseline: "Solo población",
  learned: "Aprendido (L2, entrenado 2003-2016)",
  ensemble_learned_cenepred: "Conjunto aprendido + CENEPRED",
};

interface Row {
  key: string;
  ci: Ci;
  highlight?: boolean;
}

/** AUC with its bootstrap interval for each model, on one axis from 0 to 1 with the chance line at 0.5. */
function aucChart(rows: Row[]): SVGElement {
  const rowH = 26;
  const labelW = 170;
  const width = 440;
  const plotW = width - labelW - 56;
  const height = rows.length * rowH + 24;
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "auc-chart", role: "img", "aria-label": "AUC con intervalo de confianza del 95 % por modelo" });
  const x = (v: number): number => labelW + v * plotW;
  for (const tick of [0, 0.5, 1]) {
    chart.append(svg("line", { x1: x(tick), x2: x(tick), y1: 4, y2: height - 18, class: tick === 0.5 ? "auc-chance" : "auc-grid" }));
    chart.append(svg("text", { x: x(tick), y: height - 4, class: "auc-tick", "text-anchor": "middle" }, tick.toFixed(1)));
  }
  rows.forEach((row, i) => {
    const y = 14 + i * rowH;
    chart.append(svg("text", { x: labelW - 8, y: y + 4, class: `auc-label${row.highlight ? " auc-hl" : ""}`, "text-anchor": "end" }, MODEL_LABELS[row.key] ?? row.key));
    if (row.ci.auc === null) return;
    if (row.ci.auc_ci95) {
      chart.append(svg("line", { x1: x(row.ci.auc_ci95[0]), x2: x(row.ci.auc_ci95[1]), y1: y, y2: y, class: `auc-ci${row.highlight ? " auc-hl" : ""}` }));
    }
    chart.append(svg("circle", { cx: x(row.ci.auc), cy: y, r: 4.5, class: `auc-dot${row.highlight ? " auc-hl" : ""}` }));
    chart.append(svg("text", { x: x(1) + 8, y: y + 4, class: "auc-value" }, row.ci.auc.toFixed(2)));
  });
  return chart;
}

function eventSection(key: string, event: BacktestEvent, learned: LearnedModelInfo | null): HTMLElement {
  const section = el("section", "evidence-block");
  section.append(
    el("h4", undefined, `Evento ${key}`),
    el("p", "muted", `${event.n_positive} de ${event.n_districts} distritos con emergencia de INDECI (tasa base ${(event.base_rate * 100).toFixed(0)} %).`),
  );
  const rows: Row[] = ["willay_static", "cenepred_baseline", "ensemble_static_cenepred", "population_baseline"]
    .filter((name) => event.models[name])
    .map((name) => ({ key: name, ci: event.models[name] }));
  const evaluation = learned?.evaluation[key];
  if (evaluation) {
    rows.push({ key: "learned", ci: { auc: evaluation.learned.auc, auc_ci95: evaluation.learned.auc_ci95 }, highlight: true });
    rows.push({ key: "ensemble_learned_cenepred", ci: evaluation.ensemble_learned_cenepred });
  }
  section.append(aucChart(rows) as unknown as HTMLElement);
  return section;
}

function coefficientBars(model: LearnedModelInfo["model"]): HTMLElement {
  const labels: Record<string, string> = {
    mm_high_share: "Susceptibilidad alta (INGEMMET)",
    mean_slope_deg: "Pendiente media",
    log_exposed_population: "Población expuesta (log)",
    log_tailings_upstream: "Relaves aguas arriba (log)",
  };
  const box = el("div", "coef");
  const max = Math.max(0.1, ...Object.values(model.coefficients).map((v) => Math.abs(v)));
  for (const [name, value] of Object.entries(model.coefficients)) {
    const row = el("div", "coef-row");
    const bar = el("div", `coef-bar ${value >= 0 ? "pos" : "neg"}`);
    bar.style.width = `${(Math.abs(value) / max) * 48}%`;
    bar.style.marginLeft = value >= 0 ? "50%" : `${50 - (Math.abs(value) / max) * 48}%`;
    row.append(el("span", "coef-label", labels[name] ?? name), el("div", "coef-track", ""), el("span", "coef-value", value.toFixed(2)));
    row.children[1].append(bar);
    box.append(row);
  }
  return box;
}

const MAGNITUDE_COLORS: Record<string, string> = {
  neutral: "#4b5563",
  weak: "#e0b81d",
  moderate: "#e8791c",
  strong: "#d14343",
  extraordinary: "#a21caf",
};
const MAGNITUDE_ES: Record<string, string> = { neutral: "neutral", weak: "débil", moderate: "moderada", strong: "fuerte", extraordinary: "extraordinaria" };

function enfenStrip(bulletins: EnfenBulletin[]): SVGElement {
  const width = 440;
  const height = 96;
  const strip = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "enfen-strip", role: "img", "aria-label": "Magnitud pronosticada en cada comunicado del ENFEN" });
  if (bulletins.length === 0) return strip;
  const times = bulletins.map((b) => Date.parse(b.date));
  const t0 = Math.min(...times);
  const span = Math.max(...times) - t0 || 1;
  const x = (t: number): number => 12 + ((t - t0) / span) * (width - 24);
  const levels = ["neutral", "weak", "moderate", "strong", "extraordinary"];
  strip.append(svg("line", { x1: 12, x2: width - 12, y1: height - 22, y2: height - 22, class: "auc-grid" }));
  bulletins.forEach((b) => {
    const level = Math.max(0, levels.indexOf(b.magnitude));
    const barH = 8 + level * 12;
    const bx = x(Date.parse(b.date));
    const bar = svg("rect", { x: bx - 4, y: height - 22 - barH, width: 8, height: barH, rx: 2, fill: MAGNITUDE_COLORS[b.magnitude] ?? "#6b7280" });
    bar.append(svg("title", {}, `N° ${b.number} · ${b.date} · ${MAGNITUDE_ES[b.magnitude] ?? b.magnitude} · ${b.alert_status}`));
    strip.append(bar);
  });
  strip.append(svg("text", { x: 12, y: height - 6, class: "auc-tick" }, bulletins[0].date));
  strip.append(svg("text", { x: width - 12, y: height - 6, class: "auc-tick", "text-anchor": "end" }, bulletins[bulletins.length - 1].date));
  return strip;
}

function qualityBlock(sites: Site[]): HTMLElement {
  const counts = new Map<string, number>();
  for (const site of sites) for (const flag of site.data_quality) counts.set(flag, (counts.get(flag) ?? 0) + 1);
  const block = el("section", "evidence-block");
  block.append(el("h4", undefined, "Calidad de los datos de relaves"));
  if (counts.size === 0) {
    block.append(el("p", "muted", "Sin banderas de calidad en los sitios mostrados."));
    return block;
  }
  const list = el("ul", "reasons");
  for (const [flag, count] of [...counts].sort((a, b) => b[1] - a[1])) list.append(el("li", undefined, `${count} de ${sites.length} sitios: ${dataQualityLabel(flag)}`));
  block.append(list);
  return block;
}

export async function loadEvidence(container: HTMLElement, sites: Site[]): Promise<void> {
  container.replaceChildren(el("p", "muted", "Cargando evidencia…"));
  const [backtest, learned, enfen, gauge] = await Promise.allSettled([fetchBacktest(), fetchLearnedModel(), fetchEnfen(), fetchGaugeValidation()]);
  const learnedInfo = learned.status === "fulfilled" ? learned.value : null;
  const parts: HTMLElement[] = [];

  parts.push(el("h3", undefined, "Validación contra emergencias de INDECI"));
  if (backtest.status === "fulfilled" && backtest.value.events) {
    parts.push(el("p", "muted", "AUC con intervalo bootstrap del 95 %; la línea punteada es el azar (0,5). Las etiquetas son distritos con emergencias por lluvia en INDECI."));
    for (const [key, event] of Object.entries(backtest.value.events)) parts.push(eventSection(key, event, learnedInfo));
  } else {
    parts.push(el("p", "banner", "El backtest de distritos solo existe con datos reales (scripts/run_backtest.py)."));
  }
  const reading = el("ul", "reasons");
  for (const text of [
    "2017: el puntaje estático separa bien (0,93), por encima de CENEPRED (0,77); en parte refleja la susceptibilidad de INGEMMET que ambos usan.",
    "2023: somos más débiles (0,63) y CENEPRED mejor (0,80). Con dos eventos y 78 distritos, diferencias menores a 0,1 no se distinguen.",
    "La regla PREPARATION tal como está escrita no se habría activado en 2017 ni 2023.",
  ]) reading.append(el("li", undefined, text));
  parts.push(reading);

  parts.push(el("h3", undefined, "Modelo aprendido (honesto)"));
  if (learnedInfo) {
    parts.push(
      el(
        "p",
        "muted",
        `Regresión logística L2 entrenada en temporadas ${learnedInfo.split.train_seasons[0]}-${learnedInfo.split.train_seasons[1]} y probada en ${learnedInfo.split.test_seasons.join(" y ")}. No supera al puntaje estático: su intervalo incluye 0,5.`,
      ),
      coefficientBars(learnedInfo.model),
    );
    const e17 = learnedInfo.evaluation["2017"];
    if (e17) {
      parts.push(
        el(
          "p",
          "banner",
          `Calibración: probabilidad media prevista ${(e17.learned.mean_predicted * 100).toFixed(0)} % frente a ${(e17.base_rate * 100).toFixed(0)} % observado en 2017. Es una propensión relativa de una temporada típica, no un pronóstico para un Niño. ${learnedInfo.caveat}`,
        ),
      );
    }
    parts.push(el("p", "muted", "Sin aprendizaje profundo: 78 distritos y 4 variables no dan para más; el fallo es un cambio de régimen de los datos, no de capacidad del modelo (docs/BACKTEST.md)."));
  } else {
    parts.push(el("p", "muted", "Sin modelo aprendido en este modo de datos (scripts/train_risk_model.py)."));
  }

  if (gauge.status === "fulfilled") {
    const m = gauge.value.summary;
    parts.push(
      el("h3", undefined, "Medidor de escala (conjunto público)"),
      el(
        "p",
        "muted",
        `SAM 3.1 sobre una regla graduada, calibrada con el 20 % y evaluada en ${m.n_test} imágenes retenidas: error absoluto medio ${m.mae_cm.toFixed(1)} cm (promedio constante: ${m.baseline_mae_cm.toFixed(1)} cm), ${m.within_10cm_pct.toFixed(0)} % dentro de 10 cm (${m.baseline_within_10cm_pct.toFixed(0)} %), cobertura ${m.coverage_pct.toFixed(0)} %.`,
      ),
      el("p", "banner", gauge.value.caveat),
    );
  }

  parts.push(el("h3", undefined, "Comunicados del ENFEN"));
  if (enfen.status === "fulfilled" && enfen.value.bulletins.length) {
    parts.push(enfenStrip(enfen.value.bulletins) as unknown as HTMLElement, el("p", "muted legend", `Altura = magnitud pronosticada. Fuente actual: ${enfen.value.source}.`));
  } else {
    parts.push(el("p", "muted", "Sin línea de tiempo del ENFEN en este modo de datos."));
  }

  parts.push(qualityBlock(sites));
  container.replaceChildren(...parts);
}
