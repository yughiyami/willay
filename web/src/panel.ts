import { DISTRICT_COMPONENT_LABELS, FACTOR_LABELS, dataQualityLabel } from "./labels";
import { riskBand, riskColorCss } from "./riskColor";
import type { DistrictProperties, Site, SiteDetail } from "./types";

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderSiteList(
  container: HTMLElement,
  sites: Site[],
  onSelect: (site: Site) => void,
): void {
  container.replaceChildren(
    ...sites.map((site) => {
      const item = el("li");
      const button = el("button", "site-row");
      button.type = "button";
      const swatch = el("span", "swatch");
      swatch.style.background = riskColorCss(site.risk.value);
      button.append(
        swatch,
        el("span", "site-name", `#${site.rank} ${site.name}`),
        site.data_quality.length > 0 ? el("span", "dq-dot", "!") : el("span"),
        el("span", "site-score", site.risk.value.toFixed(3)),
      );
      button.title = site.data_quality.map(dataQualityLabel).join("\n");
      button.addEventListener("click", () => onSelect(site));
      item.append(button);
      return item;
    }),
  );
}

function renderFactor(name: string, factor: Site["risk"]["factors"][string]): HTMLElement {
  const row = el("div", "factor");
  row.append(el("span", "factor-label", FACTOR_LABELS[name] ?? name));
  const track = el("div", "bar-track");
  const fill = el("div", "bar-fill");
  fill.style.width = `${Math.round(factor.value * 100)}%`;
  track.append(fill);
  row.append(track);
  const note = factor.missing
    ? `${factor.value.toFixed(2)} (missing, epsilon floor)`
    : factor.floored
      ? `${factor.value.toFixed(2)} (raw ${factor.raw?.toFixed(2)}, floored)`
      : factor.value.toFixed(2);
  row.append(el("span", "factor-value", note));
  return row;
}

function list(items: string[], className = "reasons"): HTMLUListElement {
  const ul = el("ul", className);
  for (const text of items) ul.append(el("li", undefined, text));
  return ul;
}

function formatVolume(m3: number): string {
  return m3 >= 1e6 ? `${(m3 / 1e6).toFixed(2)} Mm3` : `${Math.round(m3).toLocaleString("en-US")} m3`;
}

export function renderDetail(container: HTMLElement, site: Site, detail: SiteDetail | null): void {
  const band = riskBand(site.risk.value);
  const children: HTMLElement[] = [
    el("h2", undefined, site.name),
    el(
      "p",
      "muted",
      `${site.district}, ${site.province} - status ${site.status} - declared risk ${site.declared_risk} - ${formatVolume(site.volume_m3)}`,
    ),
  ];
  if (site.synthetic) {
    children.push(el("p", "banner", "SYNTHETIC site: invented values for development only."));
  }
  const score = el("p", "score", `Risk ${site.risk.value.toFixed(3)} (${band.label})`);
  score.style.borderLeftColor = band.css;
  children.push(score);

  if (site.data_quality.length > 0) {
    children.push(el("h3", undefined, "Data quality"));
    children.push(list(site.data_quality.map(dataQualityLabel), "reasons dq-list"));
  }

  children.push(el("h3", undefined, "Factor breakdown (product)"));
  for (const [name, factor] of Object.entries(site.risk.factors)) {
    children.push(renderFactor(name, factor));
  }

  const inputs = detail?.static_inputs;
  if (inputs && inputs.nearest_river) {
    const lines = [
      `Susceptibility: ${inputs.susceptibility_label ?? "no value"}`,
      `Nearest river or quebrada: ${inputs.nearest_river} (${Math.round(inputs.distance_to_river_m ?? 0)} m)`,
    ];
    if (inputs.flow_path_length_m) {
      lines.push(
        `Flow path: ${(inputs.flow_path_length_m / 1000).toFixed(1)} km (${inputs.flow_path_stop_reason ?? "n/a"})`,
      );
    }
    children.push(el("h3", undefined, "Static inputs"));
    children.push(list(lines));
  }

  if (detail) {
    children.push(el("h3", undefined, `Alert tier: ${detail.alert.tier}`));
    const reasons = el("ul", "reasons");
    for (const reason of detail.alert.reasons) reasons.append(el("li", undefined, reason));
    if (detail.alert.reasons.length === 0) reasons.append(el("li", undefined, "No horizon triggered."));
    children.push(reasons);
    if (detail.alert.requires_operator_confirmation) {
      children.push(el("p", "banner", "Operator confirmation required before any evacuation alert."));
    }
  }

  const settlements = detail?.exposure?.settlements ?? site.exposure?.settlements ?? [];
  if (settlements.length > 0) {
    const shown = settlements.slice(0, 12);
    children.push(el("h3", undefined, `Settlements reached (${settlements.length})`));
    const table = el("table", "reach");
    const head = el("tr");
    for (const h of ["Settlement", "Pop.", "km"]) head.append(el("th", undefined, h));
    table.append(head);
    for (const s of shown) {
      const row = el("tr");
      row.append(
        el("td", undefined, s.settlement_name),
        el("td", "num", s.population.toLocaleString("en-US")),
        el("td", "num", s.distance_km.toFixed(1)),
      );
      table.append(row);
    }
    children.push(table);
    if (settlements.length > shown.length) {
      children.push(el("p", "muted", `+ ${settlements.length - shown.length} more downstream`));
    }
  }

  const intakes = detail?.water_intakes ?? site.water_intakes ?? [];
  if (intakes.length > 0) {
    children.push(el("h3", undefined, "Water intakes reached"));
    children.push(
      list(
        intakes.map((i) => `${i.name} at ${i.distance_km.toFixed(1)} km${i.approximate ? " (approximate, verify with SEDAPAL)" : ""}`),
      ),
    );
  }

  container.replaceChildren(...children);
  container.hidden = false;
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "n/a" : `${Math.round(value * 100)}`;
}

export function renderDistrict(container: HTMLElement, d: DistrictProperties): void {
  const children: HTMLElement[] = [
    el("h2", undefined, `District ${d.name}`),
    el("p", "muted", `ubigeo ${d.ubigeo}`),
    el("p", "score", `Static score ${d.static_score.toFixed(2)} (own inputs only)`),
    el("h3", undefined, "Component percentiles"),
  ];
  for (const [name, value] of Object.entries(d.components)) {
    const row = el("div", "factor");
    row.append(el("span", "factor-label", DISTRICT_COMPONENT_LABELS[name] ?? name));
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill");
    fill.style.width = `${Math.round((value ?? 0) * 100)}%`;
    track.append(fill);
    row.append(track, el("span", "factor-value", `${pct(value)}th percentile`));
    children.push(row);
  }
  if (d.cenepred) {
    children.push(el("h3", undefined, "CENEPRED baseline (comparison only)"));
    children.push(
      list([
        `${d.cenepred.scenario}`,
        `Mass movement: ${d.cenepred.mm_risk_level} (${d.cenepred.mm_risk_value ?? "n/a"})`,
        `Flood: ${d.cenepred.flood_risk_level} (${d.cenepred.flood_risk_value ?? "n/a"})`,
      ]),
    );
  }
  children.push(el("h3", undefined, "INDECI emergencies (rain-driven types)"));
  children.push(
    list(
      Object.entries(d.emergencies).map(
        ([event, e]) => `${event}: ${e.count} record(s)${e.first_date ? `, first ${e.first_date}` : ""}`,
      ),
    ),
  );
  container.replaceChildren(...children);
  container.hidden = false;
}
