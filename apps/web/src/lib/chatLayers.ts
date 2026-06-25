import Feature, { type FeatureLike } from 'ol/Feature';
import GeoJSON from 'ol/format/GeoJSON';
import { fromExtent } from 'ol/geom/Polygon';
import WebGLTileLayer from 'ol/layer/WebGLTile';
import VectorLayer from 'ol/layer/Vector';
import type Layer from 'ol/layer/Layer';
import { asArray, type Color } from 'ol/color';
import { transformExtent } from 'ol/proj';
import GeoTIFF from 'ol/source/GeoTIFF';
import VectorSource from 'ol/source/Vector';
import { Circle as CircleStyle, Fill, Stroke, Style } from 'ol/style';
import type { StyleLike } from 'ol/style/Style';
import { resolveApiUrl } from './api';
import type { Feature as GeoJSONFeature, FeatureCollection } from 'geojson';
import type {
  Bbox,
  ChatLayer,
  ChatLayerType,
  ChatRequest,
  ChatResponse,
  Legend,
} from '@/types/chat';

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

// Severity classes missing from the legend fall back to a neutral gray.
const FALLBACK_COLOR = '#9ca3af';

const withAlpha = (color: string, alpha: number): Color => {
  const [r, g, b] = asArray(color);
  return [r, g, b, alpha];
};

// Built styles are cached by color so the per-feature style function stays cheap.
const styleByColor = new Map<string, Style>();
const colorStyle = (color: string): Style => {
  const cached = styleByColor.get(color);
  if (cached) return cached;
  // One Style covers every geometry kind: OpenLayers draws `image` for points,
  // `stroke` for lines and polygon outlines, and `fill` for polygons.
  const style = new Style({
    image: new CircleStyle({
      radius: 5,
      fill: new Fill({ color }),
      stroke: new Stroke({ color: '#ffffff', width: 1 }),
    }),
    stroke: new Stroke({ color, width: 2 }),
    fill: new Fill({ color: withAlpha(color, 0.35) }),
  });
  styleByColor.set(color, style);
  return style;
};

/**
 * Style each asset feature by its `properties.severity`, colored through the
 * legend (class -> color). Works for any geometry — point, line, polygon, or
 * their Multi* variants — via {@link colorStyle}.
 */
const severityStyle =
  (legend: Legend | null): StyleLike =>
  (feature: FeatureLike) =>
    colorStyle(legend?.[String(feature.get('severity'))]?.color ?? FALLBACK_COLOR);

/** Vector layer from a GeoJSON Feature or FeatureCollection. */
const vectorLayer = (geojson: object, style: StyleLike): Layer =>
  new VectorLayer({
    source: new VectorSource({ features: geoJSON.readFeatures(geojson, readOptions) }),
    style,
  });

/** Vector layer from a `ChatRequest.geometry` — a GeoJSON geometry or a bbox. */
const geometryLayer = (geometry: NonNullable<ChatRequest['geometry']>): Layer => {
  // A bbox is a 4-tuple, a GeoJSON geometry is an object; the tuple maps to a
  // rectangle from the (already reprojected) extent.
  const feature = Array.isArray(geometry)
    ? new Feature(fromExtent(transformExtent(geometry as Bbox, DATA_PROJECTION, VIEW_PROJECTION)))
    : new Feature(geoJSON.readGeometry(geometry, readOptions));
  return new VectorLayer({ source: new VectorSource({ features: [feature] }), style: aoiStyle });
};

// A WebGL color expression mapping each raster value to its legend class color;
// anything unmapped (including NaN nodata) renders transparent. Values are
// rounded so resampled edge pixels still land on a class.
const rasterColor = (legend: Legend) => [
  'match',
  ['round', ['band', 1]],
  ...Object.entries(legend).flatMap(([cls, { color }]) => [Number(cls), asArray(color)]),
  [0, 0, 0, 0],
];

/**
 * WebGL tile layer over the clipped hazard GeoTIFF. The AOI `bounds` are set as
 * the layer extent so it clips to the area and can be zoomed to. With a legend,
 * raw class values are colored via {@link rasterColor} (so the source's default
 * 0..1 normalization is turned off); without one it renders as a plain raster.
 */
const rasterLayer = (rasterUrl: string, bounds?: Bbox | null, legend?: Legend | null): Layer => {
  const styled = legend && Object.keys(legend).length > 0 ? legend : null;
  return new WebGLTileLayer({
    source: new GeoTIFF({ sources: [{ url: resolveApiUrl(rasterUrl) }], normalize: !styled }),
    extent: bounds ? transformExtent(bounds, DATA_PROJECTION, VIEW_PROJECTION) : undefined,
    style: styled ? { color: rasterColor(styled) } : undefined,
  });
};

const isChatResponse = (item: ChatRequest | ChatResponse): item is ChatResponse =>
  'message' in item;

// GeoJSON geometry type -> the layer type we surface; Multi* fold into their
// singular kind. Anything unmapped (e.g. GeometryCollection) -> Vector.
const GEOMETRY_TYPE: Record<string, ChatLayerType> = {
  Point: 'Point',
  MultiPoint: 'Point',
  LineString: 'LineString',
  MultiLineString: 'LineString',
  Polygon: 'Polygon',
  MultiPolygon: 'Polygon',
};

/**
 * Derive a layer type from a GeoJSON Feature or FeatureCollection: the shared
 * geometry type when every feature agrees, otherwise `Vector` as a fallback.
 */
const vectorType = (geojson: GeoJSONFeature | FeatureCollection): ChatLayerType => {
  const features = geojson.type === 'FeatureCollection' ? geojson.features : [geojson];
  const types = new Set(features.map((f) => f.geometry && GEOMETRY_TYPE[f.geometry.type]));
  return types.size === 1 ? (types.values().next().value ?? 'Vector') : 'Vector';
};

/** Layer type for a `ChatRequest.geometry` — a bbox is a Rectangle. */
const geometryType = (geometry: NonNullable<ChatRequest['geometry']>): ChatLayerType =>
  Array.isArray(geometry) ? 'Rectangle' : (GEOMETRY_TYPE[geometry.type] ?? 'Vector');

// Every layer starts half-transparent so overlapping layers stay legible.
const DEFAULT_OPACITY = 0.5;

/**
 * Build the OpenLayers layers for a chat turn from its geo fields. Layers are
 * ordered bottom-to-top: hazard raster, exposed assets, then the AOI outline.
 * A request only carries a drawn `geometry`; a response carries the rest. Each
 * layer is tagged with a name/description for the layer toggle UI; these are
 * derived from the source field for now. Everything is shown by default except
 * the AOI outline, which is opt-in.
 */
export const buildChatLayers = (item: ChatRequest | ChatResponse): ChatLayer[] => {
  const layers: ChatLayer[] = [];
  if (isChatResponse(item)) {
    const legend = item.legend ?? null;
    if (item.hazard_layer?.raster_url)
      layers.push({
        layer: rasterLayer(item.hazard_layer.raster_url, item.bounds, legend),
        type: 'Raster',
        name: 'Hazard',
        description: 'Clipped hazard severity raster.',
        visible: true,
        opacity: DEFAULT_OPACITY,
      });
    if (item.features)
      layers.push({
        layer: vectorLayer(item.features, severityStyle(legend)),
        type: vectorType(item.features),
        name: 'Assets',
        description: 'Exposed assets within the area.',
        visible: true,
        opacity: DEFAULT_OPACITY,
      });
    if (item.aoi)
      layers.push({
        layer: vectorLayer(item.aoi, aoiStyle),
        type: vectorType(item.aoi),
        name: 'Area of interest',
        description: 'Resolved AOI boundary.',
        visible: false,
        opacity: DEFAULT_OPACITY,
      });
  } else if (item.geometry) {
    layers.push({
      layer: geometryLayer(item.geometry),
      type: geometryType(item.geometry),
      name: 'Drawn area',
      description: 'User-drawn area of interest.',
      visible: true,
      opacity: DEFAULT_OPACITY,
    });
  }
  return layers;
};
