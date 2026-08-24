/**
 * Every user-facing string the trace UI invents, in one place.
 *
 * Two rules:
 *  1. `summary` and `why` come from the backend (`tracing.py` authors them per node) and
 *     are rendered verbatim. Nothing here replaces them.
 *  2. Everything the FRONTEND has to name — step titles, field labels, the reason a value
 *     is missing — lives here, not inline in a component. Re-skinning the trace, or
 *     translating it, is then a change to this file alone.
 */

import type { FinalizeStep, ResolveStep, RouterStep, TraceNode, TraceStep } from '@/types/trace';

/** Heading for the backend-authored `why` text in a step's detail panel. */
export const WHY_HEADING = 'Why this step exists';

/** Short label for the node badge on a step row. */
export const NODE_LABEL: Record<TraceNode, string> = {
  router: 'Understand',
  resolve: 'Clarify',
  fetch: 'Gather',
  operate: 'Compute',
  finalize: 'Answer',
};

/** Longer label for the same node in the execution-flow graph. */
export const GRAPH_NODE_LABEL: Record<string, string> = {
  start: 'Question',
  route: 'Understand',
  resolve: 'Clarify',
  fetch: 'Gather data',
  operate: 'Compute',
  finalize: 'Answer',
  end: 'Reply',
  ask_end: 'Waiting on you',
};

const ROUTER_TITLE: Record<RouterStep['kind'], string> = {
  routed: 'Understood your question',
  declined: 'Answered without using the data',
  missing_place: 'Needed a location',
  apply_choice: 'Applied your choice',
};

const RESOLVE_TITLE: Record<ResolveStep['decision'], string> = {
  passthrough_no_hazard: 'No hazard choice needed',
  asked: 'Asked you how to answer',
  auto_single: 'Only one way to answer this',
  no_data: 'No data for that hazard',
};

const FINALIZE_TITLE: Record<FinalizeStep['kind'], string> = {
  llm_phrase: 'Wrote the answer',
  error_echo: 'Returned the problem as-is',
};

/**
 * The end-user headline for a step. Derived from the node plus its branch tag
 * (`kind` / `decision` / the presence of an `error`) — the same discriminators the
 * backend already computes, so this never re-derives a decision, only renames it.
 */
export const stepTitle = (step: TraceStep): string => {
  switch (step.node) {
    case 'router':
      return ROUTER_TITLE[step.kind] ?? 'Understood your question';
    case 'resolve':
      if (step.decision === 'passthrough_no_hazard' && step.byod_passthrough) {
        return 'Used your uploaded layer';
      }
      return RESOLVE_TITLE[step.decision] ?? 'Chose how to answer';
    case 'fetch':
      return step.error ? "Couldn't gather the data" : 'Gathered the data';
    case 'operate':
      return step.error ? "Couldn't compute the number" : 'Computed the number';
    case 'finalize':
      return FINALIZE_TITLE[step.kind] ?? 'Wrote the answer';
    default:
      return 'Ran a step';
  }
};

/**
 * Why a field has no value. Shown as the tooltip on the `—` placeholder.
 *
 * These are the schema's own explanations, restated for a reader who is not looking
 * at the schema. Reaching for one of these is how the UI keeps "absent" and
 * "zero" distinguishable — see MISSING in `lib/trace/README.md`.
 */
export const MISSING = {
  noModelCall: 'No AI model was called in this step, so there is nothing to report here.',
  noToolSelected: 'The model answered in its own words instead of selecting a tool.',
  notApplicable: 'This does not apply to this kind of step.',
  noneRecorded: 'Nothing was recorded for this step.',
  notSupplied: 'The model did not specify one, so no threshold was applied.',
  computeFailed: 'The calculation did not complete, so there is no result.',
  noPlace: 'No place name was extracted from the question.',
} as const;

/** Human names for the AOI provenance codes `ingest.py` records in `aoi.how`. */
export const AOI_HOW_LABEL: Record<string, string> = {
  'cached AOI': 'reused an area already downloaded',
  geocoded: 'looked the place up by name',
  drawn: 'used the area you drew on the map',
};

/** Human names for the third-party services the backend can call. */
export const API_LABEL: Record<string, string> = {
  Nominatim: 'Nominatim (place lookup)',
  Overpass: 'OpenStreetMap Overpass (map features)',
  'Google Drive': 'Google Drive (hazard rasters)',
};
