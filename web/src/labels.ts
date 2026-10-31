// Plain-language labels for the data-quality flags the API returns.
export const DATA_QUALITY_LABELS: Record<string, string> = {
  status_unknown: "Operating status not reported: neutral default used",
  risk_level_unknown: "Declared risk level not reported: neutral default used",
  volume_imputed_median: "Volume missing: median of the other sites used",
  volume_estimated_from_area: "Volume missing: estimated from area x 10 m depth",
  susceptibility_unsampled: "No mass-movement susceptibility at the site: neutral value used",
  susceptibility_from_nearest_polygon: "Susceptibility taken from the nearest polygon (within 200 m)",
  no_flow_path: "No downstream flow path could be computed",
  no_site_features: "Susceptibility and river distance not computed for this site",
  minem_2011_inventory: "MINEM inventory from 2011: referential and outdated",
};

export function dataQualityLabel(flag: string): string {
  return DATA_QUALITY_LABELS[flag] ?? flag;
}

export const FACTOR_LABELS: Record<string, string> = {
  trigger: "Trigger (hazard now)",
  susceptibility: "Susceptibility",
  tailings_vulnerability: "Tailings vulnerability",
  downstream_exposure: "Downstream exposure",
};

export const DISTRICT_COMPONENT_LABELS: Record<string, string> = {
  mm_high_share: "Share of high mass-movement susceptibility",
  mean_slope_deg: "Mean slope",
  exposed_population: "Population near rivers / critical points",
  tailings_upstream: "Tailings sites upstream",
};
