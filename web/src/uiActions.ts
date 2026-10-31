import { Cartesian3, type Viewer } from "cesium";
import { highlightPath } from "./layers";
import type { LayerName, UiAction } from "./types";

/** What the assistant's `ui_actions` may do to the page. Each hook is wired in main.ts. */
export interface UiContext {
  viewer: Viewer;
  /** Select a site in the panel without moving the camera (the fly_to action owns the camera). */
  selectSite(siteId: string): void;
  setLayerVisible(layer: LayerName, visible: boolean): void;
}

const FLY_DURATION_S = 1.6;

export function executeUiActions(actions: UiAction[], context: UiContext): void {
  for (const action of actions) {
    switch (action.type) {
      case "fly_to":
        context.viewer.camera.flyTo({
          destination: Cartesian3.fromDegrees(action.lon, action.lat, action.height_m),
          duration: FLY_DURATION_S,
        });
        break;
      case "highlight_site":
        context.selectSite(action.site_id);
        break;
      case "highlight_flow_path":
        highlightPath(context.viewer, action.site_id);
        break;
      case "toggle_layer":
        context.setLayerVisible(action.layer, action.visible);
        break;
      default:
        // Unknown actions from a newer backend are ignored rather than breaking the page.
        break;
    }
  }
}
