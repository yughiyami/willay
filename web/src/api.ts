import type {
  DistrictCollection,
  FlowPathCollection,
  IntakeLayer,
  SiteDetail,
  SitesResponse,
} from "./types";

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`GET ${path} failed with HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const fetchSites = (): Promise<SitesResponse> => getJson<SitesResponse>("/sites?detail=true");

export const fetchSiteDetail = (id: string): Promise<SiteDetail> =>
  getJson<SiteDetail>(`/sites/${encodeURIComponent(id)}`);

export const fetchFlowPaths = (): Promise<FlowPathCollection> => getJson<FlowPathCollection>("/flowpaths");

export const fetchIntakes = (): Promise<IntakeLayer> => getJson<IntakeLayer>("/layers/intakes");

/** Only available with real data; the caller treats a failure as "layer not available". */
export const fetchDistricts = (): Promise<DistrictCollection> => getJson<DistrictCollection>("/districts");
