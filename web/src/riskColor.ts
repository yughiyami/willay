// Absolute thresholds on the 0-1 risk score. The product of four factors is small by design,
// so the bands are low; they are a display convention, not alert tiers.
export const RISK_BANDS = [
  { max: 0.05, label: "low", css: "#2e9e5b" },
  { max: 0.15, label: "moderate", css: "#e0b81d" },
  { max: 0.25, label: "high", css: "#e8791c" },
  { max: Number.POSITIVE_INFINITY, label: "very high", css: "#c8281e" },
] as const;

export function riskBand(value: number) {
  return RISK_BANDS.find((band) => value < band.max) ?? RISK_BANDS[RISK_BANDS.length - 1];
}

export function riskColorCss(value: number): string {
  return riskBand(value).css;
}
