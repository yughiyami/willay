import { BoundingSphere, Cartesian3, HeadingPitchRange, Math as CesiumMath, Matrix4, type Viewer } from "cesium";

/** Rimac + Lurin basins, centre of the first view (height is the approximate valley/highland terrain level). */
export const OVERVIEW = { lon: -76.55, lat: -11.88, groundM: 2500, radiusM: 30_000, rangeM: 112_000 };
/** Looking up-valley: the Rimac runs from Lima (west) towards the Andes (east-north-east). */
export const VALLEY_HEADING = CesiumMath.toRadians(65);
const TILT = CesiumMath.toRadians(-36);

export function flyToPlace(
  viewer: Viewer,
  lon: number,
  lat: number,
  options: { rangeM?: number; durationS?: number; groundM?: number; pitchDeg?: number } = {},
): void {
  const sphere = new BoundingSphere(Cartesian3.fromDegrees(lon, lat, options.groundM ?? 3200), 1500);
  viewer.camera.flyToBoundingSphere(sphere, {
    duration: options.durationS ?? 2.2,
    offset: new HeadingPitchRange(
      VALLEY_HEADING,
      options.pitchDeg !== undefined ? CesiumMath.toRadians(options.pitchDeg) : TILT,
      options.rangeM ?? 22_000,
    ),
  });
}

export function setOverview(viewer: Viewer, animate = false): void {
  const sphere = new BoundingSphere(
    Cartesian3.fromDegrees(OVERVIEW.lon, OVERVIEW.lat, OVERVIEW.groundM),
    OVERVIEW.radiusM,
  );
  const offset = new HeadingPitchRange(VALLEY_HEADING, TILT, OVERVIEW.rangeM);
  if (animate) {
    viewer.camera.flyToBoundingSphere(sphere, { duration: 2.5, offset });
    return;
  }
  viewer.camera.viewBoundingSphere(sphere, offset);
  viewer.camera.lookAtTransform(Matrix4.IDENTITY); // release the reference frame so the user can navigate freely
}
