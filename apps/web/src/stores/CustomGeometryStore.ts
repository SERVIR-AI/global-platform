import GeoJSON from 'ol/format/GeoJSON';
import type { Geometry } from 'ol/geom';
import type { Point as GeoJSONPoint, Polygon as GeoJSONPolygon } from 'geojson';
import { create } from 'zustand';

export type DrawMode = 'Point' | 'Polygon' | 'Rectangle';

interface CustomGeometryStore {
  drawMode: DrawMode | null;
  geometry: Geometry | null;
  /**
   * The draw mode that produced the current `geometry`, recorded at draw time so
   * callers don't have to re-derive it (e.g. tell a Rectangle from a freeform
   * Polygon — both are OpenLayers Polygons). Null whenever `geometry` is null.
   */
  geometryType: DrawMode | null;
  setGeometry: (geometry: Geometry | null) => void;
  setDrawMode: (drawMode: DrawMode | null) => void;
}

export const useCustomGeometryStore = create<CustomGeometryStore>((set) => ({
  drawMode: null,
  geometry: null,
  geometryType: null,
  // Capture the active drawMode as the geometry's type; the Draw `drawend`
  // handler sets the geometry before clearing drawMode, so it's still set here.
  setGeometry: (geometry) =>
    set((s) => ({ geometry, geometryType: geometry ? s.drawMode : null })),
  setDrawMode: (drawMode) => set({ drawMode }),
}));

const geoJSON = new GeoJSON();

/**
 * Serialise the stored OpenLayers geometry — held in the map view projection
 * (EPSG:3857) — into a GeoJSON Point/Polygon reprojected to EPSG:4326, the shape
 * `ChatRequest.geometry` expects. Returns null when no geometry is set.
 */
export const toChatGeometry = (
  geometry: Geometry | null,
): GeoJSONPoint | GeoJSONPolygon | null =>
  geometry
    ? (geoJSON.writeGeometryObject(geometry, {
        dataProjection: 'EPSG:4326',
        featureProjection: 'EPSG:3857',
      }) as GeoJSONPoint | GeoJSONPolygon)
    : null;
