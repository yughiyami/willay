import {
  Credit,
  Ellipsoid,
  Event as CesiumEvent,
  HeightmapTerrainData,
  Request,
  Resource,
  TerrainProvider,
  type TerrainData,
  type TilingScheme,
  WebMercatorTilingScheme,
} from "cesium";

/**
 * Token-free terrain: AWS Open Data "Terrain Tiles" (Mapzen/Terrarium PNG, SRTM + other open DEMs), decoded into
 * Cesium heightmaps. Elevation (m) = R * 256 + G + B / 256 - 32768.
 * Attribution is required by the data providers (see TERRAIN_CREDIT_HTML).
 */
const TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
const TILE_SIZE = 256;
const MAX_LEVEL = 12;
export const TERRAIN_CREDIT_HTML =
  'Relieve: <a href="https://registry.opendata.aws/terrain-tiles/" target="_blank" rel="noopener">AWS Terrain Tiles</a> (SRTM, GMTED, ETOPO1)';

export class TerrariumTerrainProvider {
  readonly tilingScheme: TilingScheme = new WebMercatorTilingScheme();
  readonly errorEvent = new CesiumEvent();
  readonly credit = new Credit(TERRAIN_CREDIT_HTML, true);
  readonly hasWaterMask = false;
  readonly hasVertexNormals = false;
  readonly availability = undefined;
  private readonly levelZeroError: number;
  private readonly scratch = typeof document !== "undefined" ? document.createElement("canvas") : null;

  constructor() {
    this.levelZeroError = TerrainProvider.getEstimatedLevelZeroGeometricErrorForAHeightmap(
      Ellipsoid.WGS84,
      128,
      this.tilingScheme.getNumberOfXTilesAtLevel(0),
    );
    if (this.scratch) {
      this.scratch.width = TILE_SIZE;
      this.scratch.height = TILE_SIZE;
    }
  }

  getLevelMaximumGeometricError(level: number): number {
    return this.levelZeroError / (1 << level);
  }

  getTileDataAvailable(_x: number, _y: number, level: number): boolean {
    return level <= MAX_LEVEL;
  }

  loadTileDataAvailability(): undefined {
    return undefined;
  }

  requestTileGeometry(x: number, y: number, level: number, request?: Request): Promise<TerrainData> | undefined {
    if (level > MAX_LEVEL || !this.scratch) return undefined;
    const url = TILE_URL.replace("{z}", String(level)).replace("{x}", String(x)).replace("{y}", String(y));
    const promise = Resource.fetchImage({ url, request, preferImageBitmap: true } as never);
    if (!promise) return undefined; // throttled: Cesium retries later
    return (promise as Promise<ImageBitmap | HTMLImageElement>).then((image) => {
      const ctx = this.scratch!.getContext("2d", { willReadFrequently: true })!;
      ctx.clearRect(0, 0, TILE_SIZE, TILE_SIZE);
      ctx.drawImage(image as CanvasImageSource, 0, 0, TILE_SIZE, TILE_SIZE);
      const rgba = ctx.getImageData(0, 0, TILE_SIZE, TILE_SIZE).data;
      const heights = new Float32Array(TILE_SIZE * TILE_SIZE);
      for (let i = 0, p = 0; i < heights.length; i += 1, p += 4) {
        heights[i] = rgba[p] * 256 + rgba[p + 1] + rgba[p + 2] / 256 - 32768;
      }
      return new HeightmapTerrainData({
        buffer: heights,
        width: TILE_SIZE,
        height: TILE_SIZE,
        childTileMask: 15,
        structure: { heightScale: 1, heightOffset: 0, elementsPerHeight: 1, stride: 1, elementMultiplier: 256, isBigEndian: false },
      } as never);
    });
  }
}
