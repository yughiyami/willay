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
  weight: number;
  impact: number;
  population: number;
  has_water_intake: boolean;
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
  synthetic: boolean;
  rank: number;
  risk: RiskBreakdown;
  static_risk: number;
  exposure: { value: number; settlements: SettlementExposure[] } | null;
}

export interface SitesResponse {
  synthetic: boolean;
  disclaimer: string;
  hazard_context: Record<string, string | number | boolean | null>;
  sites: Site[];
}

export interface SiteDetail extends Site {
  alert: {
    tier: string;
    triggered: string[];
    reasons: string[];
    requires_operator_confirmation: boolean;
  };
}
