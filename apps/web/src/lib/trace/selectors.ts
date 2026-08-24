/**
 * Envelope -> presentation-neutral view models.
 *
 * Nothing in this file knows about React, Tailwind, or SVG. That is the point: a second
 * visualization of the same trace (a printable report, a cross-turn comparison, a
 * timeline) reuses these functions verbatim and rewrites only the components. See
 * `lib/trace/README.md`.
 */

import type { TraceEnvelope, TraceNode, TraceStep } from '@/types/trace';
import { stepTitle } from './labels';

/** How a step ended. `paused` is resolve()'s human-in-the-loop stop, not a failure. */
export type StepStatus = 'ok' | 'error' | 'paused';

/** How the turn as a whole ended. */
export type TurnOutcome = 'answered' | 'paused' | 'failed';

export interface TraceSummaryView {
  stepCount: number;
  totalDurationMs: number;
  tokensTotal: number;
  costUsd: number | null;
  outcome: TurnOutcome;
  /** True if any model was called this turn — drives whether cost is worth showing. */
  usedModel: boolean;
  /** finalize's groundedness, or null when the turn never reached a phrased answer. */
  grounded: boolean | null;
}

export interface TraceStepRow {
  /** Position in `steps`; stable within a turn, and usable as a React key. */
  index: number;
  node: TraceNode | string;
  title: string;
  /** Backend-authored. Rendered verbatim. */
  summary: string;
  /** Backend-authored. Rendered verbatim. */
  why: string;
  durationMs: number;
  /** 0..1 share of the turn's total duration, for a proportional bar. */
  durationFraction: number;
  status: StepStatus;
  error: string | null;
  step: TraceStep;
}

const stepError = (step: TraceStep): string | null =>
  'error' in step && typeof step.error === 'string' ? step.error : null;

/**
 * Did this step actually call a model?
 *
 * Read `llm_provider`, never the token counts. `tokens.total === 0` is ambiguous — it
 * means either "no call" or "a call that somehow used nothing" — whereas `llm_provider`
 * is set explicitly, and only on the branches that made a call. `tokens` is now null on
 * the no-call branches too, but this stays the authoritative signal: it is the one field
 * whose whole job is to answer this question.
 */
export const stepUsedModel = (step: TraceStep): boolean =>
  'llm_provider' in step && typeof step.llm_provider === 'string';

const stepStatus = (step: TraceStep): StepStatus => {
  if (step.node === 'resolve' && step.awaiting_choice_set) return 'paused';
  return stepError(step) ? 'error' : 'ok';
};

/** Header stats for the collapsed panel. */
export const summarizeEnvelope = (envelope: TraceEnvelope): TraceSummaryView => {
  const { steps } = envelope;
  const finalize = steps.find((step) => step.node === 'finalize');
  const paused = steps.some((step) => step.node === 'resolve' && step.awaiting_choice_set);
  const failed = steps.some((step) => stepError(step) !== null);

  // Order matters: a paused turn is a deliberate stop and reads as neither answered nor
  // failed, so it wins over the error check — resolve()'s `no_data` branch sets both an
  // error and no pause, so the two never actually collide.
  const outcome: TurnOutcome = paused ? 'paused' : failed ? 'failed' : 'answered';

  return {
    stepCount: steps.length,
    totalDurationMs: envelope.total_duration,
    tokensTotal: envelope.total_tokens.total,
    costUsd: envelope.total_tokens.cost,
    outcome,
    usedModel: steps.some(stepUsedModel),
    grounded: finalize && finalize.node === 'finalize' ? finalize.grounded : null,
  };
};

/** One row per step, in execution order, with duration normalized for bar widths. */
export const toStepRows = (envelope: TraceEnvelope): TraceStepRow[] => {
  // Guard the divisor rather than the numerator: a turn served entirely from cache can
  // legitimately total ~0 ms, and every bar should then read as empty, not as NaN.
  const total = envelope.total_duration > 0 ? envelope.total_duration : 0;
  return envelope.steps.map((step, index) => ({
    index,
    node: step.node,
    title: stepTitle(step),
    summary: step.summary,
    why: step.why,
    durationMs: step.duration_ms,
    durationFraction: total > 0 ? Math.min(1, step.duration_ms / total) : 0,
    status: stepStatus(step),
    error: stepError(step),
    step,
  }));
};

/** `1,240 ms` under a second, `3.2 s` above it. */
export const formatDuration = (ms: number): string => {
  if (!Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
};

export const formatTokens = (tokens: number): string => tokens.toLocaleString();

/**
 * Costs here are fractions of a cent, so the usual 2-decimal currency format would
 * render every real value as `$0.00`. Show 4 decimals, and floor anything smaller into
 * an explicit `< $0.0001` rather than a misleading zero.
 */
export const formatCost = (usd: number | null): string => {
  if (usd === null || !Number.isFinite(usd)) return '—';
  if (usd === 0) return '$0';
  if (usd < 0.0001) return '< $0.0001';
  return `$${usd.toFixed(4)}`;
};

export const formatArea = (km2: number | null): string =>
  km2 === null || !Number.isFinite(km2) ? '—' : `${Math.round(km2).toLocaleString()} km²`;
