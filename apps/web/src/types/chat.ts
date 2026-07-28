import type { Feature, FeatureCollection, Point, Polygon } from 'geojson';
import type Layer from 'ol/layer/Layer';
import type { TraceEnvelope, TraceStep } from './trace';

export type ChatProvider = 'claude' | 'gemini' | 'openai';

export type ChatRole = 'system' | 'user' | 'assistant';

/** Asset layer the metric was computed over. */
export type AssetLayer = 'roads' | 'hospitals' | 'schools' | 'buildings';

/** `[minLon, minLat, maxLon, maxLat]` in EPSG:4326. */
export type Bbox = [number, number, number, number];

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

/** One clickable option in the agent's exposure-vs-risk ask. */
export interface ChatChoice {
  /** Layman-friendly button text. */
  label: string;
  /** What to send back as the reply when clicked (e.g. "1"). */
  value: string;
}

export interface ChatRequest {
  /** Conversation so far; at least one message is required. */
  messages: ChatMessage[];
  /** LLM provider override; defaults to the server's DEFAULT_PROVIDER. */
  provider?: ChatProvider | null;
  /** Model override; defaults to the provider's configured model. */
  model?: string | null;
  /** Stable id to continue a prior conversation; history is kept server-side by this id. */
  thread_id?: string | null;
  /** When true, the response includes `trace` — a step-by-step narration of the run. */
  verbose?: boolean;
  /**
   * Mode 2: a user-drawn AOI — a GeoJSON Polygon geometry OR a
   * `[minLon, minLat, maxLon, maxLat]` bbox, in EPSG:4326. When set, it's used
   * as the area instead of resolving a place from the message text.
   */
  geometry?: Point | Polygon | Bbox | null;
  /**
   * Optional explicit hazard (e.g. 'flood' or 'hazard_flood'), e.g. from a UI
   * button; otherwise the hazard is inferred from the text.
   */
  hazard?: string | null;
  /** ISO 8601 timestamp of when the request was created. */
  created_at?: string;
}

/** Exposure metric for the AOI × hazard × asset layer. Open-ended; server may add keys. */
export interface Metric {
  value?: number;
  unit?: string;
  total?: number;
  min_severity?: number;
  by_severity?: Record<string, number>;
  [key: string]: unknown;
}

/** A single severity-scale entry: human label + server-owned color. */
export interface LegendEntry {
  label: string;
  color: string;
}

/** Severity scale keyed by class, e.g. `{ '3': { label: 'High', color: '#...' } }`. */
export type Legend = Record<string, LegendEntry>;

/** Where the AOI boundary came from. */
export type AoiSource = 'drawn' | 'nominatim' | 'radius_box';

/** AOI boundary Feature; `properties.source` records how it was derived. */
export type AoiFeature = Feature<Polygon, { source?: AoiSource; [key: string]: unknown }>;

/** Asset features; each `properties.severity` is 0–5. */
export type AssetFeatureCollection = FeatureCollection<
  Polygon,
  { severity?: number; [key: string]: unknown }
>;

/** Hazard raster bundle: clipped GeoTIFF endpoint + vectorized polygons by class. */
export interface HazardLayer {
  /** URL of the clipped GeoTIFF; resolve against the API base with `resolveApiUrl`. */
  raster_url: string;
  /** Hazard polygons by severity class. */
  geojson: FeatureCollection;
  /** Coordinate reference system, e.g. 'EPSG:4326'. */
  crs: string;
  [key: string]: unknown;
}

export interface ChatResponse {
  id: string;
  thread_id: string;
  message: ChatMessage;
  provider: ChatProvider;
  model: string;
  usage?: Usage | null;
  /** Step-by-step narration; present only when the request set verbose=true. */
  trace?: string[] | null;
  /**
   * Structured per-step trace events for this turn — the same list as
   * `trace_envelope.steps`, without the computed header. Present unconditionally, not
   * gated by `verbose`. Kept alongside the envelope on purpose; used as a fallback when
   * envelope assembly failed server-side.
   */
  trace_events?: TraceStep[] | null;
  /**
   * The per-turn trace envelope. Best-effort — absent if assembly or persistence failed, 
   * which never affects the answer.
   * Rendered by `components/Trace`; see `lib/trace/README.md`.
   */
  trace_envelope?: TraceEnvelope | null;
  /**
   * When the agent is asking the user to choose (exposure vs precomputed-risk L1 vs
   * recomputed-risk L2), the options to render as buttons. Clicking one sends its
   * `value` (e.g. "1") as the next message on the same thread.
   */
  choices?: ChatChoice[] | null;
  /** Resolved place name, or 'drawn area'. */
  place?: string | null;
  /** Hazard layer used, e.g. 'hazard_flood'. */
  hazard?: string | null;
  /** Asset layer the metric covers. */
  layer?: AssetLayer | null;
  /** value, unit, total, min_severity, by_severity. */
  metric?: Metric | null;
  /** `{ class: { label, color } }` severity scale (server-owned colors). */
  legend?: Legend | null;
  /** `[minLon, minLat, maxLon, maxLat]` AOI bbox, for fitting the map. */
  bounds?: Bbox | null;
  /** AOI boundary as a GeoJSON Feature (drawn / nominatim / radius_box). */
  aoi?: AoiFeature | null;
  /** GeoJSON FeatureCollection of assets, each `properties.severity` 0–5. */
  features?: AssetFeatureCollection | null;
  /** Hazard raster: `{ raster_url, geojson, crs }`. */
  hazard_layer?: HazardLayer | null;
  created_at?: string;
}

/**
 * A single chat turn — the request the user sent or the response the server
 * returned — bundled with the OpenLayers layers built from its geo fields.
 * The store keeps one `ChatItem` per turn so the map can render each turn's
 * layers without re-deriving them. See `buildChatLayers`.
 */
export type ChatItem = (ChatRequest | ChatResponse) & { layers: ChatLayer[] };

/** Result of `POST /api/tiffs` — the verification report for an uploaded GeoTIFF. */
export interface TiffUploadResponse {
  /** true if the file passed verification and was registered for the thread. */
  ok: boolean;
  /** The registered layer name (e.g. `byod_flood_ab12cd34`), or null on failure. */
  layer: string | null;
  hazard_label: string;
  /** Why it failed verification (empty on pass). */
  mismatches: string[];
  /** Soft signals that didn't fail the gate (e.g. non-EPSG:4326). */
  warnings: string[];
  /** A summary of what was observed in the raster. */
  observed: {
    dtype?: string | null;
    crs_epsg?: number | null;
    sampled_min?: number | null;
    sampled_max?: number | null;
    sampled_distinct?: number | null;
    width?: number | null;
    height?: number | null;
  };
}

export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

/**
 * Kind of layer, for the layer toggle UI. Vector geometries map to their
 * GeoJSON type; `Rectangle` is a bbox, `Raster` is the hazard GeoTIFF, and
 * `Vector` is the fallback for a mixed-geometry collection.
 */
export type ChatLayerType =
  | 'Point'
  | 'LineString'
  | 'Polygon'
  | 'Rectangle'
  | 'Raster'
  | 'Vector';

export interface ChatLayer {
  layer: Layer;
  type: ChatLayerType;
  name: string;
  description: string;
  /** Whether the layer should be on the map; the map is reconciled to match. */
  visible: boolean;
  /** Layer opacity in 0..1; the reconciler applies it to the map layer. */
  opacity: number;
}
