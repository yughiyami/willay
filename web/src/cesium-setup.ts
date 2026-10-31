// Must be imported before "cesium" so the runtime finds its static assets (see vite.config.ts).
(window as unknown as { CESIUM_BASE_URL: string }).CESIUM_BASE_URL = "/cesium";
