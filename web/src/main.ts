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
import { fetchDistricts, fetchFlowPaths, fetchIntakes, fetchSiteDetail, fetchSites } from "./api";
import {
  DISTRICT_LEGENDS,
  DistrictLayer,
  type DistrictMetric,
  PATH_PREFIX,
  addFlowPaths,
  addIntakes,
  clearReachedSettlements,
  highlightPath,
  setIntakesVisible,
  setPathsVisible,
  showReachedSettlements,
} from "./layers";
import { renderDetail, renderDistrict, renderSiteList } from "./panel";
import { riskColorCss } from "./riskColor";
import type { Site } from "./types";

// Rimac basin, approximate center.
const BASIN_CENTER = { lon: -76.6, lat: -11.8 };
const SITE_PREFIX = "site-";

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
const districtSelect = document.getElementById("district-metric") as HTMLSelectElement;
const legendEl = document.getElementById("district-legend") as HTMLElement;

const districts = new DistrictLayer(viewer);

async function select(site: Site, sites: Site[]): Promise<void> {
  renderDetail(detailEl, site, null);
  highlightPath(viewer, site.id);
  showReachedSettlements(viewer, site.exposure?.settlements ?? []);
  viewer.camera.flyTo({
    destination: Cartesian3.fromDegrees(site.lon, site.lat, 60_000),
    duration: 1.2,
  });
  try {
    const detail = await fetchSiteDetail(site.id);
    renderDetail(detailEl, site, detail);
    showReachedSettlements(viewer, detail.exposure?.settlements ?? []);
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

    pathsToggle.addEventListener("change", () => setPathsVisible(viewer, pathsToggle.checked));
    intakesToggle.addEventListener("change", () => setIntakesVisible(viewer, intakesToggle.checked));
    districtSelect.addEventListener("change", () => {
      const metric = districtSelect.value as DistrictMetric;
      districts.setMetric(metric);
      legendEl.textContent = DISTRICT_LEGENDS[metric];
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
