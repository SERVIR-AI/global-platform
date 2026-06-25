import Feature from 'ol/Feature';
import GeoJSON from 'ol/format/GeoJSON';
import { fromExtent } from 'ol/geom/Polygon';
import WebGLTileLayer from 'ol/layer/WebGLTile';
import VectorLayer from 'ol/layer/Vector';
import { transformExtent } from 'ol/proj';
import GeoTIFF from 'ol/source/GeoTIFF';
import VectorSource from 'ol/source/Vector';
import { Circle as CircleStyle, Fill, Stroke, Style } from 'ol/style';
import { resolveApiUrl } from './api';
import type { Bbox, ChatRequest, ChatResponse, OLLayer } from '@/types/chat';

// The backend emits all geometry in EPSG:4326; the map view is EPSG:3857, so
// every vector source is reprojected on read.
const DATA_PROJECTION = 'EPSG:4326';
const VIEW_PROJECTION = 'EPSG:3857';
const readOptions = { dataProjection: DATA_PROJECTION, featureProjection: VIEW_PROJECTION };

const geoJSON = new GeoJSON();

// AOI / drawn boundary: blue outline, faint fill, dot for point geometries.
const aoiStyle = new Style({
  image: new CircleStyle({
    radius: 6,
    fill: new Fill({ color: 'rgba(37, 99, 235, 0.6)' }),
    stroke: new Stroke({ color: '#2563eb', width: 1.25 }),
  }),
  stroke: new Stroke({ color: '#2563eb', width: 2 }),
  fill: new Fill({ color: 'rgba(37, 99, 235, 0.1)' }),
});

// Exposed assets: orange outline so they read on top of the hazard raster.
const assetStyle = new Style({
  image: new CircleStyle({
    radius: 5,
    fill: new Fill({ color: 'rgba(234, 88, 12, 0.6)' }),
    stroke: new Stroke({ color: '#ea580c', width: 1 }),
  }),
  stroke: new Stroke({ color: '#ea580c', width: 1.5 }),
  fill: new Fill({ color: 'rgba(234, 88, 12, 0.15)' }),
});

/** Vector layer from a GeoJSON Feature or FeatureCollection. */
const vectorLayer = (geojson: object, style: Style): OLLayer =>
  new VectorLayer({
    source: new VectorSource({ features: geoJSON.readFeatures(geojson, readOptions) }),
    style,
  });

/** Vector layer from a `ChatRequest.geometry` — a GeoJSON geometry or a bbox. */
const geometryLayer = (geometry: NonNullable<ChatRequest['geometry']>): OLLayer => {
  // A bbox is a 4-tuple, a GeoJSON geometry is an object; the tuple maps to a
  // rectangle from the (already reprojected) extent.
  const feature = Array.isArray(geometry)
    ? new Feature(fromExtent(transformExtent(geometry as Bbox, DATA_PROJECTION, VIEW_PROJECTION)))
    : new Feature(geoJSON.readGeometry(geometry, readOptions));
  return new VectorLayer({ source: new VectorSource({ features: [feature] }), style: aoiStyle });
};

/** WebGL tile layer over the clipped hazard GeoTIFF. */
const rasterLayer = (rasterUrl: string): OLLayer =>
  new WebGLTileLayer({ source: new GeoTIFF({ sources: [{ url: resolveApiUrl(rasterUrl) }] }) });

const isChatResponse = (item: ChatRequest | ChatResponse): item is ChatResponse =>
  'message' in item;

/**
 * Build the OpenLayers layers for a chat turn from its geo fields. Layers are
 * ordered bottom-to-top: hazard raster, exposed assets, then the AOI outline.
 * A request only carries a drawn `geometry`; a response carries the rest.
 */
export const buildChatLayers = (item: ChatRequest | ChatResponse): OLLayer[] => {
  const layers: OLLayer[] = [];
  if (isChatResponse(item)) {
    if (item.hazard_layer?.raster_url) layers.push(rasterLayer(item.hazard_layer.raster_url));
    if (item.features) layers.push(vectorLayer(item.features, assetStyle));
    if (item.aoi) layers.push(vectorLayer(item.aoi, aoiStyle));
  } else if (item.geometry) {
    layers.push(geometryLayer(item.geometry));
  }
  return layers;
};
