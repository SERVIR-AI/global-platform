import { FC, useEffect, useRef } from 'react';
import Map from 'ol/Map';
import View from 'ol/View';
import Feature from 'ol/Feature';
import TileLayer from 'ol/layer/Tile';
import VectorLayer from 'ol/layer/Vector';
import OSM from 'ol/source/OSM';
import XYZ from 'ol/source/XYZ';
import VectorSource from 'ol/source/Vector';
import Draw, { createBox } from 'ol/interaction/Draw';
import Point from 'ol/geom/Point';
import type { FeatureLike } from 'ol/Feature';
import { Circle as CircleStyle, Fill, Stroke, Style } from 'ol/style';
import { createDefaultStyle } from 'ol/style/Style';
import 'ol/ol.css';
import { useCustomGeometryStore } from '../../stores/CustomGeometryStore';
import { useMapStore } from '../../stores/MapStore';
import BasemapSelector from './BasemapSelector';

const createBasemapSource = (basemap: 'Street' | 'Satellite') =>
  basemap === 'Satellite'
    ? new XYZ({
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attributions:
          'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community',
        maxZoom: 19,
      })
    : new OSM();

const pointStyle = new Style({
  image: new CircleStyle({
    radius: 6,
    fill: new Fill({ color: 'lightblue' }),
    stroke: new Stroke({ color: '#3399cc', width: 1.25 }),
  }),
});

const geometryStyle = (feature: FeatureLike, resolution: number) =>
  feature.getGeometry() instanceof Point ? pointStyle : createDefaultStyle(feature, resolution);

const Maps: FC = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<Map | null>(null);
  const sourceRef = useRef<VectorSource | null>(null);
  const basemapLayerRef = useRef<TileLayer | null>(null);

  const basemap = useMapStore((s) => s.basemap);
  const geometry = useCustomGeometryStore((s) => s.geometry);
  const drawMode = useCustomGeometryStore((s) => s.drawMode);
  const setGeometry = useCustomGeometryStore((s) => s.setGeometry);
  const setDrawMode = useCustomGeometryStore((s) => s.setDrawMode);

  // Initialise the map once with an OSM basemap and a vector layer for custom geometries.
  useEffect(() => {
    if (!containerRef.current) return;

    const source = new VectorSource();
    sourceRef.current = source;

    const basemapLayer = new TileLayer({
      source: createBasemapSource(useMapStore.getState().basemap),
    });
    basemapLayerRef.current = basemapLayer;

    const map = new Map({
      target: containerRef.current,
      layers: [basemapLayer, new VectorLayer({ source, style: geometryStyle })],
      view: new View({ center: [11678454, 1295712], zoom: 7 }),
    });
    mapRef.current = map;

    return () => {
      map.setTarget(undefined);
      mapRef.current = null;
      sourceRef.current = null;
      basemapLayerRef.current = null;
    };
  }, []);

  // Swap the basemap source between OSM (Street) and Esri World Imagery (Satellite).
  useEffect(() => {
    basemapLayerRef.current?.setSource(createBasemapSource(basemap));
  }, [basemap]);

  // Sync the store's geometry into the vector layer.
  useEffect(() => {
    const source = sourceRef.current;
    if (!source) return;

    source.clear();
    if (geometry) source.addFeature(new Feature(geometry));
  }, [geometry]);

  // Activate the draw interaction while a drawMode is set; clear drawMode once a shape is drawn.
  useEffect(() => {
    const map = mapRef.current;
    const source = sourceRef.current;
    if (!map || !source || !drawMode) return;

    // A rectangle is drawn as a box-constrained "Circle", which yields a Polygon geometry.
    // `freehand` makes it a press-drag-release gesture instead of the default two clicks.
    const draw = new Draw({
      source,
      type: drawMode === 'Rectangle' ? 'Circle' : drawMode,
      geometryFunction: drawMode === 'Rectangle' ? createBox() : undefined,
      freehand: drawMode === 'Rectangle',
    });
    draw.on('drawend', (event) => {
      setGeometry(event.feature.getGeometry() ?? null);
      setDrawMode(null);
    });
    map.addInteraction(draw);

    return () => {
      map.removeInteraction(draw);
    };
  }, [drawMode, setGeometry, setDrawMode]);

  return (
    <div className="basis-1 grow relative">
      {drawMode && (
        <div className="absolute top-6 w-full flex justify-center">
          <div className="badge badge-primary z-10">
            {drawMode === 'Rectangle'
              ? `Drag and drop on map to draw the ${drawMode?.toLowerCase()}`
              : `Click on map to draw the ${drawMode?.toLowerCase()}`}
          </div>
        </div>
      )}
      <div ref={containerRef} className="absolute top-0 left-0 w-full h-full" />
      <div className="absolute bottom-2 left-2 z-10">
        <BasemapSelector />
      </div>
    </div>
  );
};

export default Maps;
