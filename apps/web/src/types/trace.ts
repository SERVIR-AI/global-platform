/**
 * Wire types for the per-turn trace envelope.
 *
 * The contract is the `make_trace_event_*` builders in
 * `apps/api/src/app/graph/tracing.py`, which emit the JSON. See `lib/trace/README.md`
 * for what each field means.
 */

/** The graph nodes, as tagged in the trace. `'router'` is the route() node — see NODE_ID_BY_STEP. */
export type TraceNode = 'router' | 'resolve' | 'fetch' | 'operate' | 'finalize';

/**
 * Token counts for one model call.
 *
 * `cost_in`/`cost_out` are emitted by `tracing.py:_usage()` but are absent from the
 * schema's own `required` list, so they stay optional here.
 *
 * Caveat: on the router's `apply_choice` branch this object is present but ALL ZEROS
 * (`tracing.py:193` calls `_usage(None, ...)`), not `null`. Zero tokens there means
 * "no model was called", not "a model was called and used nothing". Use
 * `stepUsedModel()` in `lib/trace/selectors.ts` to tell the two apart — never infer it
 * from the token counts.
 */
/**
 * One model call's usage. Costs are null when the backend has no prices configured —
 * "we don't know what this cost", not "it was free".
 */
export interface StepTokens {
  in: number;
  out: number;
  total: number;
  cost: number | null;
  cost_in?: number | null;
  cost_out?: number | null;
  /** The prices the cost was computed at, per million tokens. Null when unpriced. */
  rate_usd_per_mtok?: { in: number; out: number } | null;
}

/** Envelope-level token totals. Summed in `tracing.py:build_trace_envelope`. */
export interface EnvelopeTokens {
  in: number;
  out: number;
  total: number;
  cost: number | null;
}

/** Fields every step carries, whatever its node. */
export interface TraceStepBase {
  /** 0-based index within the turn; equals the step's position in `steps`. */
  step: number;
  started_at: string;
  ended_at: string;
  /** Milliseconds, rounded to one decimal place by `tracing.py`. */
  duration_ms: number;
  /** Authored, user-facing one-liner. Presentation copy — render it, don't rewrite it. */
  summary: string;
  /** Authored explanation of why this node exists in the run. Same rule as `summary`. */
  why: string;
}

/** One entry in a router step's transcript. `tool_call` entries carry no `content`. */
/**
 * One message from the prompt actually sent to the model — never its reply. `content` is
 * null on a `tool_call` message (the call itself is in `derived_tool_calls`) and on every
 * message when the backend has `TRACE_PROMPTS` off, which keeps the roles and the count
 * but drops the bodies.
 */
export interface TraceMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  type: 'text' | 'tool_call';
  content?: string | null;
}

/** A tool call the model selected, with its arguments already parsed from JSON. */
export interface TraceToolCall {
  id: string;
  function_name: string;
  function_args: Record<string, unknown>;
}

/** What the router had available to choose from. All null on the `apply_choice` branch. */
export interface TraceAvailableAssets {
  available_tools: string[] | null;
  countable: string[] | null;
  hazard_layers: string[] | null;
  risk_layers: string[] | null;
}

/**
 * Which of route()'s four outcomes produced the step.
 * `apply_choice` is the only one with no model call.
 */
export type RouterKind = 'apply_choice' | 'declined' | 'missing_place' | 'routed';

export interface RouterStep extends TraceStepBase {
  node: 'router';
  kind: RouterKind;
  /** null on `apply_choice` — the authoritative "no model ran this turn" signal. */
  llm_provider: string | null;
  model_used: string | null;
  /** Null on `apply_choice` — that branch runs no model at all. */
  tokens: StepTokens | null;
  user_drawn_area: boolean;
  drawn_area_type: string | null;
  /** The prompt sent to the model. Null on the `apply_choice` branch — no model ran. */
  messages: TraceMessage[] | null;
  /** The model's text reply. Null when it selected a tool instead — see `derived_tool_calls`. */
  llm_response: string | null;
  available_assets: TraceAvailableAssets;
  /** null when the model replied with text instead of selecting a tool. */
  derived_tool_calls: TraceToolCall[] | null;
  derived_place: string | null;
  derived_countable_assets: string[];
  derived_hazard_layers_used: string[] | null;
  derived_risk_layers_used: string[] | null;
  error: string | null;
}

/** How resolve() settled the exposure-vs-risk question. */
export type ResolveDecision = 'passthrough_no_hazard' | 'asked' | 'auto_single' | 'no_data';

/** One exposure/risk option offered to the user. */
export interface ResolveOption {
  key: string;
  layer: string;
  label: string;
}

export interface ResolveStep extends TraceStepBase {
  node: 'resolve';
  decision: ResolveDecision;
  /** null only on `passthrough_no_hazard`. */
  hazard: string | null;
  options: ResolveOption[] | null;
  /** Only meaningful on `passthrough_no_hazard`; null on every other branch. */
  byod_passthrough: boolean | null;
  /** True only on `asked` — the graph paused here and the turn ended. */
  awaiting_choice_set: boolean;
  question_asked: string | null;
  error: string | null;
}

/** How the area of interest was determined. */
export type FetchMode = 'drawn_area' | 'place_lookup';

/** Compact AOI view — never the full path bundle. Built by `tracing.py:_summarize_aoi`. */
export interface TraceAoi {
  name: string | null;
  area_km2: number | null;
  /** e.g. 'cached AOI', 'geocoded', 'drawn'. */
  how: string | null;
}

/** An outbound call to a third-party service, recorded by `ingest.py`'s IOCollector. */
export interface TraceApiCall {
  kind: 'api';
  api: string;
  /** Nominatim only. */
  op?: string | null;
  query?: string | null;
  n_results?: number | null;
  /** Overpass only. */
  mirror_used?: string | null;
  attempts?: number | null;
  n_elements?: number | null;
}

/**
 * A file-level event: a raster download, or a clip written to disk.
 * The `downloads` field name is a slight misnomer — it is everything that isn't an
 * `api` event.
 */
/** Bytes pulled from a remote store. The only true download; clips are cache events. */
export interface TraceDownload {
  kind: 'download';
  what: 'source_raster';
  /** 'Google Drive'. */
  api?: string | null;
  layer: string;
  filename?: string | null;
  drive_id?: string | null;
  /** Cache-relative, e.g. `tiffs/hazard_flood.tif` — never the server's absolute path. */
  dest: string;
  was_cached: boolean;
}

/**
 * A local artifact reused or built. Emitted on hit AND miss, so `was_cached` answers "was
 * this fresh?" and an absent event means the check never ran — not that it missed.
 *
 * `was_cached: true` says the value was reused. It says nothing about how old it is.
 */
export interface TraceCacheCheck {
  kind: 'cache';
  what: 'aoi_boundary' | 'osm_layer' | 'hazard_clip';
  /** The AOI slug, on `aoi_boundary` only. */
  key?: string | null;
  /** Absent on `aoi_boundary`, which is not layer-scoped. */
  layer?: string | null;
  /** Cache-relative, e.g. `bangkok/roads.geojson`. */
  dest: string;
  was_cached: boolean;
}

export interface FetchStep extends TraceStepBase {
  node: 'fetch';
  mode: FetchMode;
  aoi: TraceAoi;
  /** null means every default asset layer. */
  layers_fetched: string[] | null;
  rasters_clipped: string[];
  /** risk_<hazard>_l2 layers recomputed fresh this turn. */
  l2_computed: string[];
  api_calls: TraceApiCall[] | null;
  /** Local artifacts reused or built this turn. */
  cache: TraceCacheCheck[] | null;
  downloads: TraceDownload[] | null;
  error: string | null;
}

/** The computed number and how it was produced. */
export interface OperateResult {
  method: string;
  value: number | null;
  by_severity: Record<string, number> | null;
  source: string | null;
}

export interface OperateStep extends TraceStepBase {
  node: 'operate';
  operation: string | null;
  /**
   * The severity threshold the op used. null when the model never supplied one —
   * deliberately NOT backfilled with store.py's implicit default.
   */
  min_severity: number | null;
  /** null if the op failed. */
  result: OperateResult | null;
  error: string | null;
}

/** `error_echo` returns a refusal verbatim with no model call. */
export type FinalizeKind = 'error_echo' | 'llm_phrase';

export interface FinalizeStep extends TraceStepBase {
  node: 'finalize';
  kind: FinalizeKind;
  error: string | null;
  /** null on `error_echo`. */
  llm_provider: string | null;
  model_used: string | null;
  /** Genuinely null on `error_echo` — unlike the router's zeroed object. */
  tokens: StepTokens | null;
  llm_response: string | null;
  messages: TraceMessage[] | null;
  /** True if the computed number appears verbatim in the answer text. */
  grounded: boolean | null;
}

/**
 * One step of a turn. Discriminated on `node` — a `switch` over this with a
 * `never`-typed default gets exhaustiveness checking, so adding a sixth graph node
 * fails the build at every UI that hasn't handled it. That is the entire reason this
 * is a union and not `Record<string, unknown>`.
 */
export type TraceStep = RouterStep | ResolveStep | FetchStep | OperateStep | FinalizeStep;

/** One turn's trace. Built by `tracing.py:build_trace_envelope`, one per `/api/chat` call. */
export interface TraceEnvelope {
  /** Stable across a conversation — a paused turn and its resuming turn share it. */
  thread_id: string;
  /** Per turn. Equals the `ChatResponse.id`, and names the file in `cache/traces/`. */
  trace_id: string;
  created_at: string;
  /** Milliseconds; the sum of every step's `duration`. */
  total_duration: number;
  total_tokens: EnvelopeTokens;
  steps: TraceStep[];
}
