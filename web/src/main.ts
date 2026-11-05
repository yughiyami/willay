import "./cesium-setup";
import {
  Color,
  Credit,
  ImageryLayer,
  type TerrainProvider,
  UrlTemplateImageryProvider,
  Viewer,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import "./style.css";
import "./console.css";
import "./guide.css";
import { fetchDistricts, fetchFlowPaths, fetchIntakes, fetchPasivos, fetchSiteDetail, fetchSites } from "./api";
import { initAgentConsole } from "./agentConsole";
import { initCards } from "./cards";
import { refreshChips } from "./chrome";
import { initDemo } from "./demo";
import { initIntro } from "./intro";
import { loadEvidence } from "./evidence";
import { initProposals } from "./proposalsPanel";
import { initSignals } from "./signals";
import { initTabs } from "./tabs";
import { initVigia } from "./vigia";
import { type OperatorPanel, initOperatorPanel } from "./operatorPanel";
import { executeUiActions } from "./uiActions";
import {
  DISTRICT_LEGENDS,
  DistrictLayer,
  type DistrictMetric,
  PATH_PREFIX,
  SITE_PREFIX,
  addFlowPaths,
  addSiteMarkers,
  BEACON_PREFIX,
  addIntakes,
  addPasivos,
  clearReachedSettlements,
  highlightPath,
  setIntakesVisible,
  setPasivosVisible,
  setPathsVisible,
  setReachedVisible,
  setSitesVisible,
  showHouseholdMarkers,
  showReachedSettlements,
} from "./layers";
import { flyToPlace, setOverview } from "./mapView";
import { renderDetail, renderDistrict, renderSiteList } from "./panel";
import { TerrariumTerrainProvider } from "./terrain";
import type { LayerName, Site } from "./types";

// Base map: Esri World Imagery (satellite) + Esri World Hillshade (relief shading), real terrain from AWS Terrain Tiles.
// None of these needs a Cesium ion token. Attribution is shown in the Cesium credit bar (required by the providers).
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
const satellite = new ImageryLayer(
  new UrlTemplateImageryProvider({
    url: `${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`,
    maximumLevel: 17,
    credit: new Credit("Imágenes: Esri, Maxar, Earthstar Geographics y la comunidad GIS", true),
  }),
);
const hillshade = new ImageryLayer(
  new UrlTemplateImageryProvider({
    url: `${ESRI}/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}`,
    maximumLevel: 13,
    credit: new Credit("Sombreado: Esri", true),
  }),
  { alpha: 0.4 },
);

const viewer = new Viewer("globe", {
  baseLayer: satellite,
  terrainProvider: new TerrariumTerrainProvider() as unknown as TerrainProvider,
  baseLayerPicker: false,
  geocoder: false,
  animation: false,
  timeline: false,
  homeButton: false,
  navigationHelpButton: false,
  sceneModePicker: false,
  fullscreenButton: false,
});
viewer.imageryLayers.add(hillshade);
viewer.scene.verticalExaggeration = 1.5;
viewer.scene.globe.depthTestAgainstTerrain = true;
viewer.scene.globe.maximumScreenSpaceError = 2.5; // lighter tiles, still sharp enough at this tilt
viewer.scene.globe.baseColor = Color.fromCssColorString("#1d2a1f");
viewer.scene.fog.density = 0.00012;
viewer.scene.screenSpaceCameraController.minimumZoomDistance = 600;
setOverview(viewer);
// The demo button is wired only after the map data loads; until then it stays disabled and a request is queued.
const demoButton = document.getElementById("demo-run") as HTMLButtonElement;
let demoReady = false;
let demoQueued = false;
const requestDemo = (): void => {
  if (demoReady) demoButton.click();
  else demoQueued = true;
};
initIntro(requestDemo);
(document.getElementById("view-home") as HTMLButtonElement).addEventListener("click", () => setOverview(viewer, true));

const statusEl = document.getElementById("status") as HTMLElement;
const bannerEl = document.getElementById("banner") as HTMLElement;
const listEl = document.getElementById("site-list") as HTMLElement;
const detailEl = document.getElementById("detail") as HTMLElement;
const pathsToggle = document.getElementById("toggle-paths") as HTMLInputElement;
const intakesToggle = document.getElementById("toggle-intakes") as HTMLInputElement;
const sitesToggle = document.getElementById("toggle-sites") as HTMLInputElement;
const settlementsToggle = document.getElementById("toggle-settlements") as HTMLInputElement;
const pasivosToggle = document.getElementById("toggle-pasivos") as HTMLInputElement;
const districtSelect = document.getElementById("district-metric") as HTMLSelectElement;
const legendEl = document.getElementById("district-legend") as HTMLElement;

const districts = new DistrictLayer(viewer);
let operatorPanel: OperatorPanel | null = null;

const drawerToggle = document.getElementById("drawer-toggle") as HTMLButtonElement;
const drawer = document.getElementById("map-drawer") as HTMLElement;
drawerToggle.addEventListener("click", () => {
  drawer.hidden = !drawer.hidden;
  drawerToggle.setAttribute("aria-expanded", String(!drawer.hidden));
});

async function select(site: Site, sites: Site[], fly = true): Promise<void> {
  renderDetail(detailEl, site, null);
  highlightPath(viewer, site.id);
  showReachedSettlements(viewer, site.exposure?.settlements ?? []);
  if (fly) {
    flyToPlace(viewer, site.lon, site.lat, { rangeM: 24_000 });
  }
  try {
    const detail = await fetchSiteDetail(site.id);
    renderDetail(detailEl, site, detail);
    showReachedSettlements(viewer, detail.exposure?.settlements ?? []);
    operatorPanel?.setSite(site.id, detail.alert.tier);
  } catch (error) {
    statusEl.textContent = `No se pudo cargar el detalle: ${(error as Error).message}`;
  }
  void sites;
}

async function loadOptionalLayers(sites: Site[]): Promise<void> {
  try {
    addFlowPaths(viewer, await fetchFlowPaths(), sites);
  } catch (error) {
    statusEl.textContent = `Rutas de flujo no disponibles: ${(error as Error).message}`;
  }
  try {
    addIntakes(viewer, await fetchIntakes());
    setIntakesVisible(viewer, intakesToggle.checked);
  } catch {
    intakesToggle.disabled = true;
  }
  try {
    addPasivos(viewer, await fetchPasivos());
    setPasivosVisible(viewer, pasivosToggle.checked);
  } catch {
    pasivosToggle.disabled = true;
  }
  try {
    await districts.load(await fetchDistricts());
  } catch {
    // Synthetic mode has no district data.
    districtSelect.disabled = true;
    legendEl.textContent = "La capa de distritos necesita datos reales (scripts de construcción).";
  }
}

async function main(): Promise<void> {
  try {
    const data = await fetchSites();
    statusEl.textContent = `${data.sites.length} relaves en la cuenca, ordenados por riesgo`;
    bannerEl.textContent = data.disclaimer;
    bannerEl.hidden = !data.disclaimer;
    addSiteMarkers(viewer, data.sites);
    renderSiteList(listEl, data.sites, (site) => void select(site, data.sites));
    await loadOptionalLayers(data.sites);

    operatorPanel = initOperatorPanel({
      sites: data.sites,
      onAlerts: (alerts) => showHouseholdMarkers(viewer, alerts),
      onFocus: (lon, lat) =>
        flyToPlace(viewer, lon, lat, { rangeM: 14_000 }),
    });

    sitesToggle.addEventListener("change", () => setSitesVisible(viewer, sitesToggle.checked));
    settlementsToggle.addEventListener("change", () => setReachedVisible(viewer, settlementsToggle.checked));
    pasivosToggle.addEventListener("change", () => setPasivosVisible(viewer, pasivosToggle.checked));
    pathsToggle.addEventListener("change", () => setPathsVisible(viewer, pathsToggle.checked));
    intakesToggle.addEventListener("change", () => setIntakesVisible(viewer, intakesToggle.checked));
    districtSelect.addEventListener("change", () => {
      const metric = districtSelect.value as DistrictMetric;
      districts.setMetric(metric);
      legendEl.textContent = DISTRICT_LEGENDS[metric];
    });

    const layerToggles: Partial<Record<LayerName, HTMLInputElement>> = {
      sites: sitesToggle,
      flowpaths: pathsToggle,
      settlements: settlementsToggle,
      intakes: intakesToggle,
      pasivos: pasivosToggle,
    };
    const uiContext = {
      viewer,
      selectSite: (siteId: string) => {
        const site = data.sites.find((s) => s.id === siteId);
        if (site) void select(site, data.sites, false);
      },
      setLayerVisible: (layer: LayerName, visible: boolean) => {
        if (layer === "districts") {
          if (districtSelect.disabled) return;
          districtSelect.value = visible ? "static" : "off";
          districtSelect.dispatchEvent(new Event("change"));
          return;
        }
        const toggle = layerToggles[layer];
        if (!toggle || toggle.disabled) return;
        toggle.checked = visible;
        toggle.dispatchEvent(new Event("change"));
      },
    };

    const tabs = initTabs((tab) => {
      if (tab === "alertas") {
        void proposals.refresh();
        void operatorPanel?.refresh();
      }
      if (tab === "evidencia") void signals?.refresh();
      if (tab === "evidencia" && !evidenceLoaded) {
        evidenceLoaded = true;
        void loadEvidence(document.getElementById("evidence") as HTMLElement, data.sites);
      }
    });
    let evidenceLoaded = false;
    let signals: ReturnType<typeof initSignals> | null = null;

    const alertBadge = document.getElementById("alert-badge") as HTMLElement;
    const proposals = initProposals({
      onChanged: () => void operatorPanel?.refresh(),
      onPending: (count) => {
        alertBadge.hidden = count === 0;
        alertBadge.textContent = String(count);
      },
    });

    const flyToSite = (siteId: string): void => {
      const site = data.sites.find((candidate) => candidate.id === siteId);
      if (site) void select(site, data.sites);
    };

    let demoHandle: ReturnType<typeof initDemo> | null = null;
    const agents = initAgentConsole({
      onEvent: (event) => demoHandle?.handleEvent(event),
      onRunEnd: (snapshot) => {
        if (snapshot.result?.ui_actions?.length) executeUiActions(snapshot.result.ui_actions, uiContext);
        void proposals.refresh();
        void operatorPanel?.refresh();
        void refreshChips();
      },
    });
    demoHandle = initDemo({
      agents,
      flyToSite,
      selectTab: tabs.select,
      refreshProposals: () => void proposals.refresh(),
      refreshAlerts: () => void operatorPanel?.refresh(),
      refreshChips: () => void refreshChips(),
      onStart: () => void signals?.ensureSimulated(),
    });
    demoReady = true;
    demoButton.disabled = false;
    (document.getElementById("demo-hint") as HTMLElement).textContent = "Pulse para ver el flujo completo";
    if (demoQueued) demoButton.click();
    initVigia({
      onClipRegistered: (clipId) => agents.setClipId(clipId),
      onMeasured: (rise) => {
        if (rise === null) return;
        (document.getElementById("agent-status") as HTMLElement).textContent =
          `Subida medida ${rise.toFixed(1)} cm. Pídale al Vigía que la use: el clip quedó registrado para los agentes.`;
      },
    });
    initCards(data.sites);
    signals = initSignals({
      viewer,
      flyTo: (lon, lat) => flyToPlace(viewer, lon, lat, { rangeM: 18_000 }),
    });
    void signals.refresh();
    void refreshChips();
    window.setInterval(() => void proposals.refresh(), 5000);

    viewer.selectedEntityChanged.addEventListener((entity) => {
      if (!entity) {
        highlightPath(viewer, null);
        clearReachedSettlements(viewer);
        return;
      }
      if (entity.id.startsWith(SITE_PREFIX) || entity.id.startsWith(PATH_PREFIX) || entity.id.startsWith(BEACON_PREFIX)) {
        const siteId = entity.id.replace(SITE_PREFIX, "").replace(PATH_PREFIX, "").replace(BEACON_PREFIX, "");
        const site = data.sites.find((s) => s.id === siteId);
        if (site) void select(site, data.sites);
        return;
      }
      const district = districts.propertiesOf(entity);
      if (district) renderDistrict(detailEl, district);
    });
  } catch (error) {
    statusEl.textContent = `Could not load sites. Is the backend running? (${(error as Error).message})`;
  }
}

void main();
