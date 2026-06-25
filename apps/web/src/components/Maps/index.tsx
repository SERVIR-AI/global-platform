import { FC, useEffect, useRef, useState } from 'react';
import Map from 'ol/Map';
import View from 'ol/View';
import Feature from 'ol/Feature';
import type MapBrowserEvent from 'ol/MapBrowserEvent';
import type BaseLayer from 'ol/layer/Base';
import TileLayer from 'ol/layer/Tile';
import VectorLayer from 'ol/layer/Vector';
import OSM from 'ol/source/OSM';
import XYZ from 'ol/source/XYZ';
import VectorSource from 'ol/source/Vector';
import Draw, { createBox } from 'ol/interaction/Draw';
import Point from 'ol/geom/Point';
import { transformExtent } from 'ol/proj';
import type { FeatureLike } from 'ol/Feature';
import { Circle as CircleStyle, Fill, Stroke, Style } from 'ol/style';
import { createDefaultStyle } from 'ol/style/Style';
import 'ol/ol.css';
import { useChatStore } from '../../stores/ChatStore';
import { useCustomGeometryStore } from '../../stores/CustomGeometryStore';
import { useMapStore } from '../../stores/MapStore';
import BasemapSelector from './BasemapSelector';
import MapItemProperty from './MapItemProperty';

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

const FIT_OPTIONS = { padding: [40, 40, 40, 40], duration: 300, maxZoom: 16 };

// One readout row under the cursor: a chat layer's name, plus the hit feature's
// `name` (vector only) and its severity class.
type HoveredItem = { layerName: string; itemName?: string; severity: number };

const Maps: FC = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<Map | null>(null);
  const sourceRef = useRef<VectorSource | null>(null);
  const basemapLayerRef = useRef<TileLayer | null>(null);
  const fittedIdRef = useRef<string | null>(null);

  const basemap = useMapStore((s) => s.basemap);
  const map = useMapStore((s) => s.map);
  const messages = useChatStore((s) => s.messages);
  const geometry = useCustomGeometryStore((s) => s.geometry);
  const drawMode = useCustomGeometryStore((s) => s.drawMode);
  const setGeometry = useCustomGeometryStore((s) => s.setGeometry);
  const setDrawMode = useCustomGeometryStore((s) => s.setDrawMode);

  const [hovered, setHovered] = useState<HoveredItem[]>([]);

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
    useMapStore.getState().setMap(map);

    return () => {
      map.setTarget(undefined);
      mapRef.current = null;
      sourceRef.current = null;
      basemapLayerRef.current = null;
      useMapStore.getState().setMap(null);
    };
  }, []);

  // Swap the basemap source between OSM (Street) and Esri World Imagery (Satellite).
  useEffect(() => {
    basemapLayerRef.current?.setSource(createBasemapSource(basemap));
  }, [basemap]);

  // Reconcile chat layers onto the map: every visible layer is present, every
  // chat-managed layer that's now hidden (or gone) is removed — base layers
  // (basemap, drawn geometry) are left untouched. Re-runs on message/visibility
  // changes and whenever the map is (re)built, so toggles and panel remounts
  // both apply.
  useEffect(() => {
    if (!map) return;
    const chatLayers = messages.flatMap((m) => m.layers);
    const managed = new Set<BaseLayer>(chatLayers.map((l) => l.layer));
    const desired = new Set<BaseLayer>(chatLayers.filter((l) => l.visible).map((l) => l.layer));
    const collection = map.getLayers();
    collection
      .getArray()
      .filter((l) => managed.has(l) && !desired.has(l))
      .forEach((l) => collection.remove(l));
    desired.forEach((l) => {
      if (!collection.getArray().includes(l)) collection.push(l);
    });
    chatLayers.forEach((l) => {
      if (l.visible) l.layer.setOpacity(l.opacity);
    });
  }, [map, messages]);

  // Show a property readout for whatever sits under the cursor: hit vector
  // features (name + severity) and hazard raster pixels (severity only). Rebinds
  // when the layer set changes so the layer->name lookup stays current.
  useEffect(() => {
    if (!map) return;
    const chatLayers = messages.flatMap((m) => m.layers);
    const chatLayerFor = (layer: BaseLayer | null) =>
      layer ? chatLayers.find((l) => l.layer === layer) : undefined;

    const handleMove = (event: MapBrowserEvent) => {
      if (event.dragging) {
        setHovered([]);
        return;
      }
      const items: HoveredItem[] = [];

      // Vector hits first (topmost feature first) so they sit above raster rows.
      map.forEachFeatureAtPixel(
        event.pixel,
        (feature, layer) => {
          const chatLayer = chatLayerFor(layer);
          const severity = feature.get('severity');
          if (!chatLayer || typeof severity !== 'number') return;
          const name = feature.get('name');
          items.push({
            layerName: chatLayer.name,
            itemName: typeof name === 'string' ? name : undefined,
            severity,
          });
        },
        { layerFilter: (l) => !!chatLayerFor(l) },
      );

      // Raster pixels: read the source band value (rounded to its severity class).
      chatLayers.forEach((chatLayer) => {
        if (chatLayer.type !== 'Raster' || !chatLayer.visible) return;
        const data = chatLayer.layer.getData(event.pixel);
        if (!data || data instanceof DataView) return;
        const value = data[0];
        if (Number.isFinite(value) && value > 0)
          items.push({ layerName: chatLayer.name, severity: Math.round(value) });
      });

      setHovered(items);
    };

    map.on('pointermove', handleMove);
    return () => map.un('pointermove', handleMove);
  }, [map, messages]);

  // Fit the view to a newly appended response's `bounds`. Guarded on the
  // response id so it fires once per response, not on every layer toggle.
  useEffect(() => {
    if (!map) return;
    const last = messages[messages.length - 1];
    if (!last || !('id' in last) || !last.bounds || last.id === fittedIdRef.current) return;
    fittedIdRef.current = last.id;
    map.getView().fit(transformExtent(last.bounds, 'EPSG:4326', 'EPSG:3857'), FIT_OPTIONS);
  }, [map, messages]);

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
      <div className="absolute bottom-2 left-2 z-10 flex flex-col gap-2">
        {hovered.length > 0 && (
          <div className="flex flex-col gap-1">
            {hovered.map((item, index) => (
              <MapItemProperty
                key={index}
                layerName={item.layerName}
                itemName={item.itemName}
                severity={item.severity}
              />
            ))}
          </div>
        )}
        <BasemapSelector />
      </div>
    </div>
  );
};

export default Maps;
