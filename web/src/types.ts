export interface FactorBreakdown {
  raw: number | null;
  value: number;
  floored: boolean;
  missing: boolean;
}

export interface RiskBreakdown {
  value: number;
  factors: Record<string, FactorBreakdown>;
  missing_factors: string[];
}

export interface SettlementExposure {
  settlement_id: string;
  settlement_name: string;
  along_path_km: number;
  distance_km: number;
  weight: number;
  impact: number;
  population: number;
  has_water_intake: boolean;
  lon: number;
  lat: number;
}

export interface IntakeExposure {
  intake_id: string;
  name: string;
  operator: string;
  lon: number;
  lat: number;
  distance_km: number;
  snap_distance_m: number;
  impact: number;
  approximate: boolean;
}

export interface Site {
  id: string;
  name: string;
  lon: number;
  lat: number;
  district: string;
  province: string;
  status: string;
  declared_risk: string;
  volume_m3: number;
  area_m2: number | null;
  synthetic: boolean;
  in_basin: boolean;
  data_quality: string[];
  rank: number;
  risk: RiskBreakdown;
  static_risk: number;
  exposure: { value: number; settlements: SettlementExposure[] } | null;
  water_intakes: IntakeExposure[];
}

export interface SitesResponse {
  synthetic: boolean;
  data_mode: string;
  disclaimer: string;
  official_source?: string;
  hazard_context: Record<string, string | number | boolean | null>;
  sites: Site[];
}

export interface FlowPathFeature {
  type: "Feature";
  geometry: { type: "LineString"; coordinates: number[][] };
  properties: {
    site_id: string;
    name: string;
    length_m: number;
    stop_reason?: string;
    cumulative_distance_m: number[];
  };
}

export interface FlowPathCollection {
  type: "FeatureCollection";
  features: FlowPathFeature[];
}

export interface StaticInputs {
  susceptibility_class?: number | null;
  susceptibility_label?: string | null;
  susceptibility_method?: string;
  distance_to_river_m?: number;
  nearest_river?: string;
  flow_path_stop_reason?: string | null;
  flow_path_length_m?: number | null;
}

export interface SiteDetail extends Site {
  alert: {
    tier: string;
    triggered: string[];
    reasons: string[];
    requires_operator_confirmation: boolean;
  };
  flow_path: FlowPathFeature | null;
  static_inputs: StaticInputs;
}

export interface IntakeItem {
  id: string;
  name: string;
  lon: number;
  lat: number;
  operator: string;
  kind: string;
  approximate: boolean;
}

export interface IntakeLayer {
  note: string;
  items: IntakeItem[];
}

export interface EmergencySummary {
  count: number;
  first_date: string | null;
}

export interface DistrictProperties {
  ubigeo: string;
  name: string;
  static_score: number;
  components: Record<string, number | null>;
  missing_components: string[];
  features: Record<string, number | string | Record<string, number> | null>;
  cenepred: {
    baseline_only: boolean;
    scenario: string;
    flood_risk_value: number | null;
    flood_risk_level: string;
    mm_risk_value: number | null;
    mm_risk_level: string;
    population_2017: number;
  } | null;
  emergencies: Record<string, EmergencySummary>;
}

export interface DistrictCollection {
  type: "FeatureCollection";
  scoring: { weights: Record<string, number>; method: string; emergency_hazards: string[] };
  features: { type: "Feature"; geometry: unknown; properties: DistrictProperties }[];
}
