import "./cesium-setup";
import {
  Cartesian3,
  Color,
  EllipsoidTerrainProvider,
  ImageryLayer,
  OpenStreetMapImageryProvider,
  Viewer,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import "./style.css";
import { fetchDistricts, fetchFlowPaths, fetchIntakes, fetchPasivos, fetchSiteDetail, fetchSites } from "./api";
import { initAgentPanel } from "./agentPanel";
import { type OperatorPanel, initOperatorPanel } from "./operatorPanel";
import { executeUiActions } from "./uiActions";
import {
  DISTRICT_LEGENDS,
  DistrictLayer,
  type DistrictMetric,
  PATH_PREFIX,
  SITE_PREFIX,
  addFlowPaths,
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
import { renderDetail, renderDistrict, renderSiteList } from "./panel";
import { riskColorCss } from "./riskColor";
import type { LayerName, Site } from "./types";

// Rimac basin, approximate center.
const BASIN_CENTER = { lon: -76.6, lat: -11.8 };

const viewer = new Viewer("globe", {
  baseLayer: new ImageryLayer(
    new OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" }),
  ),
  terrainProvider: new EllipsoidTerrainProvider(),
  baseLayerPicker: false,
  geocoder: false,
  animation: false,
  timeline: false,
  homeButton: false,
  navigationHelpButton: false,
  sceneModePicker: false,
  fullscreenButton: false,
});

viewer.camera.setView({
  destination: Cartesian3.fromDegrees(BASIN_CENTER.lon, BASIN_CENTER.lat, 150_000),
});

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

async function select(site: Site, sites: Site[], fly = true): Promise<void> {
  renderDetail(detailEl, site, null);
  highlightPath(viewer, site.id);
  showReachedSettlements(viewer, site.exposure?.settlements ?? []);
  if (fly) {
    viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(site.lon, site.lat, 60_000),
      duration: 1.2,
    });
  }
  try {
    const detail = await fetchSiteDetail(site.id);
    renderDetail(detailEl, site, detail);
    showReachedSettlements(viewer, detail.exposure?.settlements ?? []);
    operatorPanel?.setSite(site.id, detail.alert.tier);
  } catch (error) {
    statusEl.textContent = `Could not load detail: ${(error as Error).message}`;
  }
  void sites;
}

async function loadOptionalLayers(sites: Site[]): Promise<void> {
  try {
    addFlowPaths(viewer, await fetchFlowPaths(), sites);
  } catch (error) {
    statusEl.textContent = `Flow paths unavailable: ${(error as Error).message}`;
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
    legendEl.textContent = "District layer needs real data (run the build scripts).";
  }
}

async function main(): Promise<void> {
  try {
    const data = await fetchSites();
    statusEl.textContent = `${data.sites.length} tailings sites in the basin, ranked by risk`;
    bannerEl.textContent = data.disclaimer;
    bannerEl.hidden = !data.disclaimer;
    for (const site of data.sites) {
      viewer.entities.add({
        id: `${SITE_PREFIX}${site.id}`,
        name: site.name,
        position: Cartesian3.fromDegrees(site.lon, site.lat),
        point: {
          pixelSize: 16,
          color: Color.fromCssColorString(riskColorCss(site.risk.value)),
          outlineColor: Color.WHITE,
          outlineWidth: 2,
        },
      });
    }
    renderSiteList(listEl, data.sites, (site) => void select(site, data.sites));
    await loadOptionalLayers(data.sites);

    operatorPanel = initOperatorPanel({
      sites: data.sites,
      onAlerts: (alerts) => showHouseholdMarkers(viewer, alerts),
      onFocus: (lon, lat) =>
        viewer.camera.flyTo({ destination: Cartesian3.fromDegrees(lon, lat, 25_000), duration: 1.2 }),
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
    initAgentPanel({
      onReply: (reply) =>
        executeUiActions(reply.ui_actions, {
          viewer,
          selectSite: (siteId) => {
            const site = data.sites.find((s) => s.id === siteId);
            if (site) void select(site, data.sites, false);
          },
          setLayerVisible: (layer, visible) => {
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
        }),
    });

    viewer.selectedEntityChanged.addEventListener((entity) => {
      if (!entity) {
        highlightPath(viewer, null);
        clearReachedSettlements(viewer);
        return;
      }
      if (entity.id.startsWith(SITE_PREFIX) || entity.id.startsWith(PATH_PREFIX)) {
        const siteId = entity.id.replace(SITE_PREFIX, "").replace(PATH_PREFIX, "");
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
