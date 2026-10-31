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
import { fetchSiteDetail, fetchSites } from "./api";
import { renderDetail, renderSiteList } from "./panel";
import { riskColorCss } from "./riskColor";
import type { Site } from "./types";

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
  destination: Cartesian3.fromDegrees(BASIN_CENTER.lon, BASIN_CENTER.lat, 90_000),
});

const statusEl = document.getElementById("status") as HTMLElement;
const bannerEl = document.getElementById("banner") as HTMLElement;
const listEl = document.getElementById("site-list") as HTMLElement;
const detailEl = document.getElementById("detail") as HTMLElement;

async function select(site: Site): Promise<void> {
  renderDetail(detailEl, site, null);
  viewer.camera.flyTo({
    destination: Cartesian3.fromDegrees(site.lon, site.lat, 25_000),
    duration: 1.2,
  });
  try {
    renderDetail(detailEl, site, await fetchSiteDetail(site.id));
  } catch (error) {
    statusEl.textContent = `Could not load detail: ${(error as Error).message}`;
  }
}

async function main(): Promise<void> {
  try {
    const data = await fetchSites();
    statusEl.textContent = `${data.sites.length} sites, ranked by risk`;
    if (data.synthetic) {
      bannerEl.textContent = data.disclaimer;
      bannerEl.hidden = false;
    }
    for (const site of data.sites) {
      viewer.entities.add({
        id: site.id,
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
    renderSiteList(listEl, data.sites, (site) => void select(site));
    viewer.selectedEntityChanged.addEventListener((entity) => {
      const site = entity && data.sites.find((s) => s.id === entity.id);
      if (site) void select(site);
    });
  } catch (error) {
    statusEl.textContent = `Could not load sites. Is the backend running? (${(error as Error).message})`;
  }
}

void main();
