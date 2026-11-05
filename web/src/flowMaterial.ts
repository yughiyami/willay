import { Color, Event as CesiumEvent, Material, type JulianDate } from "cesium";

/**
 * Animated "water flowing downstream" polyline material (works on ground-clamped polylines):
 * a soft glowing band with bright pulses travelling along the path. Pure GLSL, driven by czm_frameNumber.
 */
export const FLOW_MATERIAL_TYPE = "WillayFlow";

const SOURCE = `
czm_material czm_getMaterial(czm_materialInput materialInput) {
  czm_material material = czm_getDefaultMaterial(materialInput);
  vec2 st = materialInput.st;
  float t = fract(st.s * cycles - czm_frameNumber * 0.010);
  float pulse = smoothstep(0.0, 0.12, t) * (1.0 - smoothstep(0.30, 0.75, t));
  float edge = clamp(1.0 - abs(st.t - 0.5) * 2.0, 0.0, 1.0);
  float body = pow(edge, 0.55);
  material.diffuse = mix(color.rgb, vec3(1.0), pulse * 0.55);
  material.emission = color.rgb * (0.35 + pulse * 0.9);
  material.alpha = color.a * body * (0.55 + 0.45 * pulse);
  return material;
}`;

let registered = false;
function register(): void {
  if (registered) return;
  registered = true;
  (Material as unknown as { _materialCache: { addMaterial(type: string, value: unknown): void } })._materialCache.addMaterial(
    FLOW_MATERIAL_TYPE,
    {
      fabric: { type: FLOW_MATERIAL_TYPE, uniforms: { color: new Color(1, 1, 1, 1), cycles: 8.0 }, source: SOURCE },
      translucent: () => true,
    },
  );
}

/** A real MaterialProperty (getType/getValue/equals/definitionChanged/isConstant), as entity graphics require. */
export class FlowMaterialProperty {
  readonly isConstant = true;
  readonly definitionChanged = new CesiumEvent();

  constructor(
    private color: Color,
    private cycles: number,
  ) {
    register();
  }

  getType(_time?: JulianDate): string {
    return FLOW_MATERIAL_TYPE;
  }

  getValue(_time?: JulianDate, result: Record<string, unknown> = {}): Record<string, unknown> {
    result.color = this.color;
    result.cycles = this.cycles;
    return result;
  }

  equals(other: unknown): boolean {
    return this === other || (other instanceof FlowMaterialProperty && Color.equals(this.color, other.color) && this.cycles === other.cycles);
  }
}
