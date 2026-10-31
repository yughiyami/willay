import { riskBand, riskColorCss } from "./riskColor";
import type { Site, SiteDetail } from "./types";

const FACTOR_LABELS: Record<string, string> = {
  trigger: "Trigger (hazard now)",
  susceptibility: "Susceptibility",
  tailings_vulnerability: "Tailings vulnerability",
  downstream_exposure: "Downstream exposure",
};

function el<K extends keyof HTMLElementTagNameMap>(
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
        el("span", "site-score", site.risk.value.toFixed(3)),
      );
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

export function renderDetail(container: HTMLElement, site: Site, detail: SiteDetail | null): void {
  const band = riskBand(site.risk.value);
  const children: HTMLElement[] = [
    el("h2", undefined, site.name),
    el(
      "p",
      "muted",
      `${site.district}, ${site.province} - ${site.status} - declared ${site.declared_risk}`,
    ),
  ];
  if (site.synthetic) {
    children.push(el("p", "banner", "SYNTHETIC site: invented values for development only."));
  }
  const score = el("p", "score", `Risk ${site.risk.value.toFixed(3)} (${band.label})`);
  score.style.borderLeftColor = band.css;
  children.push(score);
  children.push(el("h3", undefined, "Factor breakdown (product)"));
  for (const [name, factor] of Object.entries(site.risk.factors)) {
    children.push(renderFactor(name, factor));
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
    if (detail.exposure && detail.exposure.settlements.length > 0) {
      children.push(el("h3", undefined, "Downstream settlements"));
      const list = el("ul", "reasons");
      for (const s of detail.exposure.settlements) {
        const intake = s.has_water_intake ? ", water intake" : "";
        list.append(
          el("li", undefined, `${s.settlement_name}: ${s.along_path_km.toFixed(1)} km, pop. ${s.population}${intake}`),
        );
      }
      children.push(list);
    }
  }
  container.replaceChildren(...children);
  container.hidden = false;
}
