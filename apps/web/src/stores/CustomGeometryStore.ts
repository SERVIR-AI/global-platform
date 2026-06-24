import type { Geometry } from 'ol/geom';
import { create } from 'zustand';

type DrawMode = 'Point' | 'Polygon' | 'Rectangle' | null;

interface CustomGeometryStore {
  drawMode: DrawMode;
  geometry: Geometry | null;
  setGeometry: (geometry: Geometry | null) => void;
  setDrawMode: (drawMode: DrawMode) => void;
}

export const useCustomGeometryStore = create<CustomGeometryStore>((set) => ({
  drawMode: null,
  geometry: null,
  setGeometry: (geometry) => set({ geometry }),
  setDrawMode: (drawMode) => set({ drawMode }),
}));
