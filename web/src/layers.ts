import {
  Cartesian2,
  Cartesian3,
  Color,
  ConstantProperty,
  GeoJsonDataSource,
  LabelStyle,
  type Entity,
  type Viewer,
} from "cesium";
import { riskColorCss } from "./riskColor";
import type {
  DistrictCollection,
  DistrictProperties,
  FlowPathCollection,
  HouseholdAlert,
  IntakeLayer,
  PasivosLayer,
  Site,
  SettlementExposure,
} from "./types";

export type DistrictMetric = "off" | "static" | "learned" | "cenepred" | "emergency2017" | "emergency2023";

export const PATH_PREFIX = "path-";
export const INTAKE_PREFIX = "intake-";

export function addFlowPaths(viewer: Viewer, paths: FlowPathCollection, sites: Site[]): void {
  const riskBySite = new Map(sites.map((s) => [s.id, s.risk.value]));
  for (const feature of paths.features) {
    const siteId = feature.properties.site_id;
    const risk = riskBySite.get(siteId);
    if (risk === undefined) continue; // path of a site outside the ranked set
    const flat = feature.geometry.coordinates.flat();
    viewer.entities.add({
      id: `${PATH_PREFIX}${siteId}`,
      name: `Flow path of ${feature.properties.name}`,
      polyline: {
        positions: Cartesian3.fromDegreesArray(flat),
        width: 2,
        clampToGround: true,
        material: Color.fromCssColorString(riskColorCss(risk)).withAlpha(0.75),
      },
    });
  }
}

export function highlightPath(viewer: Viewer, siteId: string | null): void {
  for (const entity of viewer.entities.values) {
    if (!entity.id.startsWith(PATH_PREFIX) || !entity.polyline) continue;
    entity.polyline.width = new ConstantProperty(entity.id === `${PATH_PREFIX}${siteId}` ? 5 : 2);
  }
}

export function addIntakes(viewer: Viewer, layer: IntakeLayer): void {
  for (const item of layer.items) {
    viewer.entities.add({
      id: `${INTAKE_PREFIX}${item.id}`,
      name: `${item.name} (approximate)`,
      position: Cartesian3.fromDegrees(item.lon, item.lat),
      point: {
        pixelSize: 9,
        color: Color.fromCssColorString("#2fa8e8"),
        outlineColor: Color.WHITE,
        outlineWidth: 2,
      },
      label: {
        text: `${item.name} (approx.)`,
        font: "11px sans-serif",
        style: LabelStyle.FILL_AND_OUTLINE,
        outlineWidth: 3,
        fillColor: Color.WHITE,
        outlineColor: Color.BLACK,
        pixelOffset: new Cartesian2(10, -10),
        showBackground: false,
        distanceDisplayCondition: undefined,
      },
    });
  }
}

export function setIntakesVisible(viewer: Viewer, visible: boolean): void {
  for (const entity of viewer.entities.values) {
    if (entity.id.startsWith(INTAKE_PREFIX)) entity.show = visible;
  }
}

export function setPathsVisible(viewer: Viewer, visible: boolean): void {
  for (const entity of viewer.entities.values) {
    if (entity.id.startsWith(PATH_PREFIX)) entity.show = visible;
  }
}

const REACHED_PREFIX = "reached-";
export const SITE_PREFIX = "site-";
export const PASIVO_PREFIX = "pasivo-";
let reachedVisible = true;

export function setSitesVisible(viewer: Viewer, visible: boolean): void {
  for (const entity of viewer.entities.values) {
    if (entity.id.startsWith(SITE_PREFIX)) entity.show = visible;
  }
}

export function setReachedVisible(viewer: Viewer, visible: boolean): void {
  reachedVisible = visible;
  for (const entity of viewer.entities.values) {
    if (entity.id.startsWith(REACHED_PREFIX)) entity.show = visible;
  }
}

/** MINEM 2011 liabilities: a secondary, referential layer (hidden until asked for). */
export function addPasivos(viewer: Viewer, layer: PasivosLayer): void {
  layer.items.forEach((item, index) => {
    viewer.entities.add({
      id: `${PASIVO_PREFIX}${item.id ?? index}`,
      name: `${item.name} (MINEM 2011, referential)`,
      show: false,
      position: Cartesian3.fromDegrees(item.lon, item.lat),
      point: {
        pixelSize: 6,
        color: Color.fromCssColorString("#a98467"),
        outlineColor: Color.WHITE,
        outlineWidth: 1,
      },
    });
  });
}

export function setPasivosVisible(viewer: Viewer, visible: boolean): void {
  for (const entity of viewer.entities.values) {
    if (entity.id.startsWith(PASIVO_PREFIX)) entity.show = visible;
  }
}

export function showReachedSettlements(viewer: Viewer, settlements: SettlementExposure[]): void {
  clearReachedSettlements(viewer);
  for (const s of settlements) {
    if (!s.lon && !s.lat) continue;
    viewer.entities.add({
      id: `${REACHED_PREFIX}${s.settlement_id}`,
      show: reachedVisible,
      name: `${s.settlement_name} (pop. ${s.population})`,
      position: Cartesian3.fromDegrees(s.lon, s.lat),
      point: {
        pixelSize: Math.max(5, Math.min(18, 3 + Math.log10(s.population + 10) * 3)),
        color: Color.fromCssColorString("#f4f1ea").withAlpha(0.9),
        outlineColor: Color.fromCssColorString("#7a3b00"),
        outlineWidth: 2,
      },
    });
  }
}

export function clearReachedSettlements(viewer: Viewer): void {
  for (const entity of [...viewer.entities.values]) {
    if (entity.id.startsWith(REACHED_PREFIX)) viewer.entities.remove(entity);
  }
}

// --- district choropleth ---------------------------------------------------------

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

/** Sequential pale-yellow to dark-red ramp for values in [0, 1]. */
export function rampColor(value: number): Color {
  const t = Math.max(0, Math.min(1, value));
  return new Color(lerp(1.0, 0.62, t), lerp(0.93, 0.05, t), lerp(0.72, 0.08, t), 0.55);
}

export function districtMetricValue(p: DistrictProperties, metric: DistrictMetric): number | null {
  switch (metric) {
    case "static":
      return p.static_score;
    case "learned":
      return p.learned?.percentile_rank ?? null;
    case "cenepred": {
      const values = [p.cenepred?.flood_risk_value, p.cenepred?.mm_risk_value].filter(
        (v): v is number => typeof v === "number",
      );
      return values.length ? values.reduce((a, b) => a + b, 0) / values.length / 5 : null;
    }
    case "emergency2017":
      return (p.emergencies["2017"]?.count ?? 0) > 0 ? 1 : 0;
    case "emergency2023":
      return (p.emergencies["2023"]?.count ?? 0) > 0 ? 1 : 0;
    default:
      return null;
  }
}

export const DISTRICT_LEGENDS: Record<DistrictMetric, string> = {
  off: "",
  static: "Static score 0-1 (percentile composite of our own inputs; no outcome or CENEPRED data)",
  learned: "Modelo aprendido (regresión logística L2, entrenado 2003-2016): rango percentil de la propensión de una temporada típica. No superó al puntaje estático en 2017/2023 (ver Evidencia).",
  cenepred: "CENEPRED El Nino 2027 scenario, mean of flood and mass-movement risk / 5 (comparison baseline)",
  emergency2017: "Dark = at least one INDECI rain-driven emergency, Jan-Apr 2017",
  emergency2023: "Dark = at least one INDECI rain-driven emergency, Mar 2023",
};

export class DistrictLayer {
  private source: GeoJsonDataSource | null = null;
  private metric: DistrictMetric = "off";

  constructor(private readonly viewer: Viewer) {}

  async load(collection: DistrictCollection): Promise<void> {
    this.source = await GeoJsonDataSource.load(collection as unknown as object, {
      stroke: Color.fromCssColorString("#27303c"),
      strokeWidth: 1,
      fill: Color.TRANSPARENT,
      clampToGround: true,
    });
    this.source.show = false;
    await this.viewer.dataSources.add(this.source);
  }

  get loaded(): boolean {
    return this.source !== null;
  }

  setMetric(metric: DistrictMetric): void {
    this.metric = metric;
    if (!this.source) return;
    this.source.show = metric !== "off";
    for (const entity of this.source.entities.values) {
      const props = this.propertiesOf(entity);
      if (!props || !entity.polygon) continue;
      const value = districtMetricValue(props, metric);
      entity.polygon.material = new ConstantProperty(
        value === null ? Color.TRANSPARENT : rampColor(value),
      ) as never;
    }
  }

  propertiesOf(entity: Entity): DistrictProperties | null {
    if (!this.source || !this.source.entities.contains(entity)) return null;
    const bag = entity.properties;
    if (!bag) return null;
    const now = this.viewer.clock.currentTime;
    const result: Record<string, unknown> = {};
    for (const name of bag.propertyNames) result[name] = bag[name]?.getValue(now);
    return result as unknown as DistrictProperties;
  }

  get currentMetric(): DistrictMetric {
    return this.metric;
  }
}

// --- alert loop: households that need a human ---------------------------------------

export const HOUSEHOLD_PREFIX = "household-";

/**
 * Highlights the settlements where a synthetic household asked for help (red, large) or could not be reached
 * (amber). Households that answered SAFE are not drawn. Replaces any previous markers.
 */
export function showHouseholdMarkers(viewer: Viewer, alerts: HouseholdAlert[]): void {
  for (const entity of [...viewer.entities.values]) {
    if (entity.id.startsWith(HOUSEHOLD_PREFIX)) viewer.entities.remove(entity);
  }
  const bySettlement = new Map<string, { lon: number; lat: number; name: string; help: number; silent: number }>();
  for (const alert of alerts) {
    if (alert.response !== "NEEDS_HELP" && alert.response !== "NO_ANSWER") continue;
    const key = `${alert.lon.toFixed(5)},${alert.lat.toFixed(5)}`;
    const entry = bySettlement.get(key) ?? { lon: alert.lon, lat: alert.lat, name: alert.settlement_name, help: 0, silent: 0 };
    if (alert.response === "NEEDS_HELP") entry.help += 1;
    else entry.silent += 1;
    bySettlement.set(key, entry);
  }
  for (const [key, entry] of bySettlement) {
    const needsHelp = entry.help > 0;
    const text = needsHelp
      ? `${entry.name}: ${entry.help} hogar(es) piden ayuda`
      : `${entry.name}: ${entry.silent} sin respuesta`;
    viewer.entities.add({
      id: `${HOUSEHOLD_PREFIX}${key}`,
      name: text,
      position: Cartesian3.fromDegrees(entry.lon, entry.lat),
      point: {
        pixelSize: needsHelp ? 24 : 15,
        color: Color.fromCssColorString(needsHelp ? "#e5383b" : "#f59e0b"),
        outlineColor: Color.WHITE,
        outlineWidth: needsHelp ? 4 : 2,
      },
      label: needsHelp
        ? {
            text,
            font: "13px sans-serif",
            style: LabelStyle.FILL_AND_OUTLINE,
            outlineWidth: 3,
            fillColor: Color.WHITE,
            outlineColor: Color.fromCssColorString("#7a0c10"),
            pixelOffset: new Cartesian2(14, -14),
            showBackground: false,
          }
        : undefined,
    });
  }
}
