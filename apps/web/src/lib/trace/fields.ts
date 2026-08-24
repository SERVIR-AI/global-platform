/**
 * Per-node field descriptors: what to show, to whom, and what it means.
 *
 * This is the file that decides the audience split. Every field is tagged `user` or
 * `developer` as DATA, so the two views can never drift apart the way two hand-written
 * components would, and a third audience later is a new tag rather than a new pipeline.
 *
 * A missing value is never rendered as `0` or as blank. It becomes a
 * `{ kind: 'missing', reason }` carrying an explanation, because this schema's
 * nullability is load-bearing — see `lib/trace/README.md`.
 */

import type {
  FetchStep,
  FinalizeStep,
  OperateStep,
  ResolveStep,
  RouterStep,
  StepTokens,
  TraceApiCall,
  TraceCacheCheck,
  TraceDownload,
  TraceMessage,
  TraceStep,
} from '@/types/trace';
import { API_LABEL, AOI_HOW_LABEL, MISSING } from './labels';
import { formatArea, formatCost, stepUsedModel } from './selectors';

export type FieldAudience = 'user' | 'developer';

/**
 * A value in a form a renderer can draw without knowing which field it came from.
 * Keep this union small — every member is a rendering branch that a re-visualization
 * has to reimplement.
 */
export type TraceFieldValue =
  | { kind: 'text'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'list'; items: string[] }
  | { kind: 'flag'; value: boolean; label: string }
  | { kind: 'missing'; reason: string }
  | { kind: 'transcript'; messages: TraceMessage[] }
  | { kind: 'json'; value: unknown };

export interface TraceField {
  key: string;
  label: string;
  audience: FieldAudience;
  /** Tooltip. On a missing value this is what explains the `—`. */
  hint?: string;
  value: TraceFieldValue;
}

export interface TraceFieldGroup {
  group: string;
  fields: TraceField[];
}

// --- small constructors, so the descriptor tables below stay readable ---

const text = (value: string): TraceFieldValue => ({ kind: 'text', text: value });
const code = (value: string): TraceFieldValue => ({ kind: 'code', text: value });
const json = (value: unknown): TraceFieldValue => ({ kind: 'json', value });
const missing = (reason: string): TraceFieldValue => ({ kind: 'missing', reason });
const flag = (value: boolean, label: string): TraceFieldValue => ({ kind: 'flag', value, label });

/** A message transcript, or an explained absence when there's nothing to show. */
const transcript = (
  messages: TraceMessage[] | null | undefined,
  reason: string,
): TraceFieldValue =>
  messages && messages.length > 0 ? { kind: 'transcript', messages } : missing(reason);

/** A list, or an explained absence — an empty array is an absence, not an empty list. */
const list = (items: string[] | null | undefined, reason: string): TraceFieldValue =>
  items && items.length > 0 ? { kind: 'list', items } : missing(reason);

/** A string, or an explained absence. */
const maybeText = (value: string | null | undefined, reason: string): TraceFieldValue =>
  value ? text(value) : missing(reason);

const field = (
  key: string,
  label: string,
  audience: FieldAudience,
  value: TraceFieldValue,
  hint?: string,
): TraceField => ({ key, label, audience, value, hint });

/** Drop empty groups so the UI never renders a heading with nothing under it. */
const filterGroups = (groups: TraceFieldGroup[], detail: FieldAudience): TraceFieldGroup[] =>
  groups
    .map((group) => ({
      group: group.group,
      fields: group.fields.filter((f) => detail === 'developer' || f.audience === 'user'),
    }))
    .filter((group) => group.fields.length > 0);

// --- shared renderings ---

/**
 * Token counts, but only when a model actually ran.
 *
 * `usedModel` is passed in rather than inferred from the token counts: a real call that
 * returned nothing and a branch that made no call would both read as zero. `llm_provider`
 * is the authoritative "did a model run" signal.
 */
const tokenFields = (tokens: StepTokens | null, usedModel: boolean): TraceField[] => {
  if (!usedModel || !tokens) {
    return [
      field('tokens', 'Tokens', 'developer', missing(MISSING.noModelCall)),
      field('cost', 'Cost', 'developer', missing(MISSING.noModelCall)),
    ];
  }
  return [
    field(
      'tokens',
      'Tokens',
      'developer',
      text(`${tokens.in.toLocaleString()} in · ${tokens.out.toLocaleString()} out`),
    ),
    field('cost', 'Cost', 'developer', text(formatCost(tokens.cost))),
  ];
};

const describeApiCall = (call: TraceApiCall): string => {
  const label = API_LABEL[call.api] ?? call.api;
  if (call.query) return `${label} — looked up "${call.query}" (${call.n_results ?? 0} matches)`;
  if (call.n_elements !== undefined && call.n_elements !== null) {
    return `${label} — returned ${call.n_elements.toLocaleString()} features`;
  }
  return label;
};

/**
 * Cached-vs-fetched is the provenance question an analyst actually asks, so it leads the
 * sentence; the local path it landed at is a developer concern and is dropped here.
 */
const describeDownload = (event: TraceDownload): string =>
  `${event.layer} — downloaded, ${event.was_cached ? 'already had it' : 'fetched fresh'}`;

const CACHE_LABEL: Record<TraceCacheCheck['what'], string> = {
  aoi_boundary: 'area boundary',
  osm_layer: 'map features',
  hazard_clip: 'hazard layer cropped to your area',
};

/** Same question as a download, for artifacts built locally rather than pulled down. */
const describeCacheCheck = (event: TraceCacheCheck): string => {
  const what = CACHE_LABEL[event.what] ?? event.what;
  const subject = event.layer ? `${event.layer} — ${what}` : what;
  return `${subject}, ${event.was_cached ? 'reused' : 'built fresh'}`;
};

const describeBySeverity = (bySeverity: Record<string, number> | null): TraceFieldValue =>
  bySeverity && Object.keys(bySeverity).length > 0
    ? {
        kind: 'list',
        items: Object.entries(bySeverity).map(
          ([klass, value]) => `Severity ${klass}: ${Number(value).toLocaleString()}`,
        ),
      }
    : missing(MISSING.notApplicable);

// --- per-node descriptors ---

const routerFields = (step: RouterStep): TraceFieldGroup[] => {
  const usedModel = stepUsedModel(step);
  const call = step.derived_tool_calls?.[0] ?? null;
  return [
    {
      group: 'What it understood',
      fields: [
        field('place', 'Place', 'user', maybeText(step.derived_place, MISSING.noPlace)),
        field(
          'assets',
          'Things counted',
          'user',
          list(step.derived_countable_assets, MISSING.notApplicable),
        ),
        field(
          'hazards',
          'Hazard layers',
          'user',
          list(step.derived_hazard_layers_used, MISSING.notApplicable),
        ),
        field(
          'risks',
          'Risk layers',
          'user',
          list(step.derived_risk_layers_used, MISSING.notApplicable),
        ),
        field(
          'calculation',
          'Calculation chosen',
          'user',
          call ? code(call.function_name) : missing(MISSING.noToolSelected),
        ),
        field(
          'drawn',
          'Area you drew',
          'user',
          step.user_drawn_area
            ? text(step.drawn_area_type ? `Yes — ${step.drawn_area_type}` : 'Yes')
            : missing('You asked by place name rather than drawing on the map.'),
        ),
        field('error', 'Problem', 'user', maybeText(step.error, MISSING.noneRecorded)),
      ],
    },
    {
      group: 'Model call',
      fields: [
        field(
          'provider',
          'Provider',
          'developer',
          maybeText(step.llm_provider, MISSING.noModelCall),
        ),
        field('model', 'Model', 'developer', maybeText(step.model_used, MISSING.noModelCall)),
        ...tokenFields(step.tokens, usedModel),
        field(
          'tool_args',
          'Tool arguments',
          'developer',
          call ? json(call.function_args) : missing(MISSING.noToolSelected),
        ),
        field(
          'tools_offered',
          'Tools offered',
          'developer',
          list(step.available_assets.available_tools, MISSING.noModelCall),
        ),
        field(
          'llm_response',
          'Text reply',
          'developer',
          maybeText(step.llm_response, MISSING.noToolSelected),
        ),
        field(
          'transcript',
          'Prompt sent',
          'developer',
          transcript(step.messages, MISSING.noneRecorded),
        ),
      ],
    },
  ];
};

const resolveFields = (step: ResolveStep): TraceFieldGroup[] => [
  {
    group: 'Choosing how to answer',
    fields: [
      field('hazard', 'Hazard', 'user', maybeText(step.hazard, MISSING.notApplicable)),
      field(
        'options',
        'Options offered',
        'user',
        list(
          step.options?.map((option) => option.label),
          'There was nothing to choose between.',
        ),
      ),
      field(
        'question',
        'Question asked',
        'user',
        maybeText(step.question_asked, 'No question was needed.'),
      ),
      field(
        'paused',
        'Paused for you',
        'user',
        flag(step.awaiting_choice_set, step.awaiting_choice_set ? 'Yes' : 'No'),
      ),
      field(
        'byod',
        'Your uploaded layer',
        'user',
        step.byod_passthrough === null
          ? missing(MISSING.notApplicable)
          : flag(step.byod_passthrough, step.byod_passthrough ? 'Used directly' : 'Not used'),
      ),
      field('error', 'Problem', 'user', maybeText(step.error, MISSING.noneRecorded)),
      field('decision', 'Decision code', 'developer', code(step.decision)),
      field(
        'option_layers',
        'Backing layers',
        'developer',
        list(
          step.options?.map((option) => `${option.key} → ${option.layer}`),
          MISSING.notApplicable,
        ),
      ),
    ],
  },
];

const fetchFields = (step: FetchStep): TraceFieldGroup[] => [
  {
    group: 'Area of interest',
    fields: [
      field('aoi_name', 'Area', 'user', maybeText(step.aoi.name, MISSING.noneRecorded)),
      field(
        'aoi_area',
        'Size',
        'user',
        step.aoi.area_km2 === null
          ? missing(MISSING.noneRecorded)
          : text(formatArea(step.aoi.area_km2)),
      ),
      field(
        'aoi_how',
        'How it was found',
        'user',
        step.aoi.how
          ? text(AOI_HOW_LABEL[step.aoi.how] ?? step.aoi.how)
          : missing(MISSING.noneRecorded),
      ),
      field('mode', 'Mode', 'developer', code(step.mode)),
    ],
  },
  {
    group: 'Where the data came from',
    fields: [
      field(
        'api_calls',
        'Services consulted',
        'user',
        list(step.api_calls?.map(describeApiCall), 'No outside services were needed.'),
        'Third-party services the backend called while assembling this answer.',
      ),
      field(
        'downloads',
        'Files downloaded',
        'user',
        list(step.downloads?.map(describeDownload), 'Nothing had to be downloaded.'),
        '"Already had it" means the file was reused from the local cache rather than re-downloaded.',
      ),
      field(
        'cache',
        'Prepared locally',
        'user',
        list(step.cache?.map(describeCacheCheck), 'Nothing was prepared locally.'),
        'Artifacts built on this machine rather than downloaded. "Reused" means a previous run had already built it.',
      ),
    ],
  },
  {
    group: 'Layers prepared',
    fields: [
      field(
        'rasters',
        'Hazard layers cropped',
        'user',
        list(step.rasters_clipped, 'None were needed.'),
      ),
      field(
        'l2',
        'Risk layers recomputed',
        'user',
        list(step.l2_computed, 'None were recomputed for this answer.'),
      ),
      field(
        'assets',
        'Map features fetched',
        'user',
        list(step.layers_fetched, 'Every default feature layer was fetched.'),
      ),
      field('error', 'Problem', 'user', maybeText(step.error, MISSING.noneRecorded)),
    ],
  },
];

const operateFields = (step: OperateStep): TraceFieldGroup[] => [
  {
    group: 'The number',
    fields: [
      field(
        'value',
        'Result',
        'user',
        step.result && step.result.value !== null
          ? text(step.result.value.toLocaleString())
          : missing(MISSING.computeFailed),
      ),
      field(
        'method',
        'How it was calculated',
        'user',
        step.result ? code(step.result.method) : missing(MISSING.computeFailed),
        'A fixed calculation over the map data. No AI model is involved in this step.',
      ),
      field(
        'source',
        'Read from',
        'user',
        step.result
          ? maybeText(step.result.source, MISSING.noneRecorded)
          : missing(MISSING.computeFailed),
      ),
      field(
        'by_severity',
        'Broken down by severity',
        'user',
        step.result ? describeBySeverity(step.result.by_severity) : missing(MISSING.computeFailed),
      ),
      field(
        'min_severity',
        'Severity threshold',
        'user',
        step.min_severity === null ? missing(MISSING.notSupplied) : text(String(step.min_severity)),
      ),
      field('error', 'Problem', 'user', maybeText(step.error, MISSING.noneRecorded)),
      field('operation', 'Operation', 'developer', maybeText(step.operation, MISSING.noneRecorded)),
      field(
        'result_raw',
        'Raw result',
        'developer',
        step.result ? json(step.result) : missing(MISSING.computeFailed),
      ),
    ],
  },
];

const finalizeFields = (step: FinalizeStep): TraceFieldGroup[] => {
  const usedModel = stepUsedModel(step);
  return [
    {
      group: 'The answer',
      fields: [
        field(
          'grounded',
          'Backed by the computed number',
          'user',
          step.grounded === null
            ? missing(
                step.kind === 'error_echo'
                  ? 'No number was computed, so there was nothing to check the answer against.'
                  : MISSING.noneRecorded,
              )
            : flag(
                step.grounded,
                step.grounded
                  ? 'Yes — the number appears in the answer'
                  : 'No — the number does not appear verbatim',
              ),
          'Checks that the calculated figure actually appears in the wording you were shown.',
        ),
        field(
          'wrote',
          'Wording',
          'user',
          step.kind === 'error_echo'
            ? text('Returned as-is, with no AI rewording.')
            : text('An AI model phrased the result. It did not calculate the number.'),
        ),
        field('error', 'Problem', 'user', maybeText(step.error, MISSING.noneRecorded)),
        field(
          'provider',
          'Provider',
          'developer',
          maybeText(step.llm_provider, MISSING.noModelCall),
        ),
        field('model', 'Model', 'developer', maybeText(step.model_used, MISSING.noModelCall)),
        ...tokenFields(step.tokens, usedModel),
        field(
          'llm_response',
          'Answer text',
          'developer',
          maybeText(step.llm_response, MISSING.noneRecorded),
        ),
        field(
          'transcript',
          'Prompt sent',
          'developer',
          transcript(step.messages, MISSING.noModelCall),
        ),
      ],
    },
  ];
};

/**
 * Everything worth showing about one step, grouped and filtered for an audience.
 *
 * The `default` branch is deliberately reachable at runtime even though `tsc` proves it
 * isn't: an envelope written by a newer backend can carry a node this build has never
 * heard of. The union catches that at compile time for code we control; this catches it
 * at runtime for data we don't.
 */
export const toStepFields = (step: TraceStep, detail: FieldAudience): TraceFieldGroup[] => {
  switch (step.node) {
    case 'router':
      return filterGroups(routerFields(step), detail);
    case 'resolve':
      return filterGroups(resolveFields(step), detail);
    case 'fetch':
      return filterGroups(fetchFields(step), detail);
    case 'operate':
      return filterGroups(operateFields(step), detail);
    case 'finalize':
      return filterGroups(finalizeFields(step), detail);
    default: {
      const unknownStep: never = step;
      return filterGroups(
        [
          {
            group: 'Unrecognized step',
            fields: [
              field(
                'raw',
                'Raw data',
                'user',
                json(unknownStep),
                'This step came from a newer version of the backend than this page knows about.',
              ),
            ],
          },
        ],
        detail,
      );
    }
  }
};
