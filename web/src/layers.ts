import {
  Cartesian2,
  Cartesian3,
  Color,
  ColorMaterialProperty,
  ConstantProperty,
  DistanceDisplayCondition,
  GeoJsonDataSource,
  HeightReference,
  LabelStyle,
  type Entity,
  type Viewer,
} from "cesium";
import { FlowMaterialProperty } from "./flowMaterial";
import type { SignalDistrict } from "./consoleTypes";
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
export const BEACON_PREFIX = "beacon-";

/** Everything that sits on the map is clamped to the terrain and never hidden behind a ridge. */
const ON_GROUND = {
  heightReference: HeightReference.CLAMP_TO_GROUND,
  disableDepthTestDistance: Number.POSITIVE_INFINITY,
} as const;

const LABEL_BACKGROUND = Color.fromCssColorString("#0b0f14").withAlpha(0.72);

function mapLabel(text: string, options: { font?: string; fill?: string; offset?: Cartesian2; maxDistanceM?: number } = {}) {
  return {
    text,
    font: options.font ?? "600 13px 'Segoe UI', system-ui, sans-serif",
    style: LabelStyle.FILL,
    fillColor: Color.fromCssColorString(options.fill ?? "#ffffff"),
    showBackground: true,
    backgroundColor: LABEL_BACKGROUND,
    backgroundPadding: new Cartesian2(7, 4),
    pixelOffset: options.offset ?? new Cartesian2(0, -22),
    heightReference: HeightReference.CLAMP_TO_GROUND,
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
    distanceDisplayCondition: new DistanceDisplayCondition(0, options.maxDistanceM ?? 160_000),
  };
}

/** Tailings as risk-coloured glowing markers: a point on the ground plus a translucent beacon column. */
export function addSiteMarkers(viewer: Viewer, sites: Site[]): void {
  const labelled = new Set<string>();
  for (const site of sites) {
    const color = Color.fromCssColorString(riskColorCss(site.risk.value));
    const labelText = labelled.has(site.name) ? null : site.name;
    labelled.add(site.name);
    const marker = viewer.entities.add({
      id: `${SITE_PREFIX}${site.id}`,
      name: site.name,
      position: Cartesian3.fromDegrees(site.lon, site.lat),
      point: { pixelSize: 16, color, outlineColor: Color.WHITE, outlineWidth: 2, ...ON_GROUND },
      label: labelText ? mapLabel(labelText, { maxDistanceM: 90_000 }) : undefined,
    });
    viewer.entities.add({
      id: `${BEACON_PREFIX}${site.id}`,
      name: site.name,
      parent: marker,
      position: Cartesian3.fromDegrees(site.lon, site.lat),
      ellipse: {
        semiMajorAxis: 260 + site.risk.value * 1500,
        semiMinorAxis: 260 + site.risk.value * 1500,
        heightReference: HeightReference.CLAMP_TO_GROUND,
        extrudedHeight: 500 + site.risk.value * 6500,
        extrudedHeightReference: HeightReference.RELATIVE_TO_GROUND,
        material: new ColorMaterialProperty(color.withAlpha(0.38)),
        outline: false,
      },
    });
  }
}

export function addFlowPaths(viewer: Viewer, paths: FlowPathCollection, sites: Site[]): void {
  const riskBySite = new Map(sites.map((s) => [s.id, s.risk.value]));
  for (const feature of paths.features) {
    const siteId = feature.properties.site_id;
    const risk = riskBySite.get(siteId);
    if (risk === undefined) continue; // path of a site outside the ranked set
    const flat = feature.geometry.coordinates.flat();
    const color = Color.fromCssColorString(riskColorCss(risk));
    viewer.entities.add({
      id: `${PATH_PREFIX}${siteId}`,
      name: `Ruta de flujo de ${feature.properties.name}`,
      polyline: {
        positions: Cartesian3.fromDegreesArray(flat),
        width: 7,
        clampToGround: true,
        // A real MaterialProperty: animated glowing flow in the risk colour (a raw ConstantProperty would crash the renderer).
        material: new FlowMaterialProperty(color, Math.max(3, (feature.properties.length_m ?? 20_000) / 2500)) as never,
      },
    });
  }
}

export function highlightPath(viewer: Viewer, siteId: string | null): void {
  for (const entity of viewer.entities.values) {
    if (!entity.id.startsWith(PATH_PREFIX) || !entity.polyline) continue;
    entity.polyline.width = new ConstantProperty(entity.id === `${PATH_PREFIX}${siteId}` ? 12 : 7);
  }
}

export function addIntakes(viewer: Viewer, layer: IntakeLayer): void {
  for (const item of layer.items) {
    const isAtarjea = item.name === "La Atarjea";
    const unnamed = /^unnamed/i.test(item.name);
    viewer.entities.add({
      id: `${INTAKE_PREFIX}${item.id}`,
      name: `${item.name} (aprox.)`,
      position: Cartesian3.fromDegrees(item.lon, item.lat),
      point: {
        pixelSize: isAtarjea ? 20 : 11,
        color: Color.fromCssColorString("#2fc4ff"),
        outlineColor: Color.WHITE,
        outlineWidth: isAtarjea ? 3 : 2,
        ...ON_GROUND,
      },
      label: unnamed
        ? undefined
        : mapLabel(isAtarjea ? "La Atarjea · agua potable de Lima" : `${item.name} (aprox.)`, {
            fill: "#8fe0ff",
            font: isAtarjea ? "700 15px 'Segoe UI', system-ui, sans-serif" : undefined,
            maxDistanceM: isAtarjea ? 400_000 : 70_000,
            offset: new Cartesian2(0, isAtarjea ? -30 : -20),
          }),
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
        ...ON_GROUND,
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
        pixelSize: Math.max(8, Math.min(20, 4 + Math.log10(s.population + 10) * 3)),
        color: Color.fromCssColorString("#ffd166"),
        outlineColor: Color.fromCssColorString("#7a3b00"),
        outlineWidth: 2,
        ...ON_GROUND,
      },
      label: mapLabel(`${s.settlement_name} · ${s.population} hab.`, {
        fill: "#ffe3a3",
        font: "600 12px 'Segoe UI', system-ui, sans-serif",
        offset: new Cartesian2(0, -18),
        maxDistanceM: 70_000,
      }),
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
      // Must be a MaterialProperty. A bare ConstantProperty(Color) has no getType() and stops the renderer.
      entity.polygon.material = new ColorMaterialProperty(value === null ? Color.TRANSPARENT : rampColor(value));
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
        ...ON_GROUND,
      },
      label: needsHelp ? mapLabel(text, { fill: "#ffd1d3", offset: new Cartesian2(0, -50), maxDistanceM: 400_000 }) : undefined,
    });
  }
}

// --- public signals (unverified; hollow markers so they never read as a measured risk) ----------------------------------

export const SIGNAL_PREFIX = "signal-";
const SIGNAL_COLOR = "#9ad1ff";

/** One hollow marker per district with reports. Replaces the previous set. */
export function showSignals(viewer: Viewer, districts: SignalDistrict[], visible: boolean): void {
  for (const entity of [...viewer.entities.values]) {
    if (entity.id.startsWith(SIGNAL_PREFIX)) viewer.entities.remove(entity);
  }
  for (const district of districts) {
    viewer.entities.add({
      id: `${SIGNAL_PREFIX}${district.ubigeo || district.district}`,
      name: `Señales públicas no verificadas · ${district.district}${district.simulated ? " (simulado)" : ""}: ${district.count}`,
      show: visible,
      position: Cartesian3.fromDegrees(district.lon, district.lat),
      point: {
        pixelSize: 12 + Math.min(district.count, 6) * 3,
        color: Color.fromCssColorString(SIGNAL_COLOR).withAlpha(0.18),
        outlineColor: Color.fromCssColorString(SIGNAL_COLOR),
        outlineWidth: district.simulated ? 2 : 3,
        ...ON_GROUND,
      },
      label: mapLabel(`${district.count}${district.simulated ? " sim." : ""}`, {
        fill: SIGNAL_COLOR,
        font: "600 11px 'Segoe UI', system-ui, sans-serif",
        offset: new Cartesian2(16, 10),
        maxDistanceM: 120_000,
      }),
    });
  }
}

export function setSignalsVisible(viewer: Viewer, visible: boolean): void {
  for (const entity of viewer.entities.values) {
    if (entity.id.startsWith(SIGNAL_PREFIX)) entity.show = visible;
  }
}
