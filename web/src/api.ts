import type { SiteDetail, SitesResponse } from "./types";

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`GET ${path} failed with HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const fetchSites = (): Promise<SitesResponse> => getJson<SitesResponse>("/sites");

export const fetchSiteDetail = (id: string): Promise<SiteDetail> =>
  getJson<SiteDetail>(`/sites/${encodeURIComponent(id)}`);
