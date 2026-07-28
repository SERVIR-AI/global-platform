/**
 * Shape-guards for the trace envelope.
 *
 * The backend builds and persists the envelope inside a bare `except`
 * (`chat.py:97-104`) precisely so a tracing bug can never break the answer. This module
 * is the client half of that stance: everything here returns `null` or a repaired value
 * rather than throwing, so a malformed or stale envelope costs one missing panel, never
 * a blank chat.
 */

import type { EnvelopeTokens, TraceEnvelope, TraceStep } from '@/types/trace';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * Deliberately lenient: a step needs only the fields every renderer relies on. An
 * unrecognized `node` still passes, because `TraceStepDetail`'s fallback branch can
 * render it — a schema that has run ahead of this UI should degrade, not disappear.
 */
const isTraceStep = (value: unknown): value is TraceStep =>
  isRecord(value) &&
  typeof value.node === 'string' &&
  typeof value.summary === 'string' &&
  typeof value.duration === 'number' &&
  Number.isFinite(value.duration);

/**
 * Sum token counts the way `tracing.py:build_trace_envelope` does: include every step
 * that carries a tokens object at all, skip the ones that carry none. A step with a
 * zeroed tokens object (the router's `apply_choice` branch) is included and contributes
 * zeros — matching the backend exactly, so the client-side fallback total can never
 * disagree with the server-side one.
 */
const sumTokens = (steps: TraceStep[]): EnvelopeTokens => {
  const carried = steps
    .map((step) => ('tokens' in step ? step.tokens : null))
    .filter((tokens): tokens is NonNullable<typeof tokens> => isRecord(tokens));
  return {
    in: carried.reduce((sum, t) => sum + (t.in || 0), 0),
    out: carried.reduce((sum, t) => sum + (t.out || 0), 0),
    total: carried.reduce((sum, t) => sum + (t.total || 0), 0),
    cost: carried.reduce((sum, t) => sum + (t.cost || 0), 0),
  };
};

const sumDuration = (steps: TraceStep[]): number =>
  steps.reduce((sum, step) => sum + (step.duration || 0), 0);

/**
 * Validate a raw `trace_envelope` and return it typed, or `null` if it isn't one.
 *
 * `total_duration` and `total_tokens` are recomputed when absent or non-numeric rather
 * than treated as fatal — they are derivable from `steps`, so a missing header is a
 * cosmetic defect, not a reason to drop the whole trace.
 */
export const parseEnvelope = (raw: unknown): TraceEnvelope | null => {
  if (!isRecord(raw) || !Array.isArray(raw.steps)) return null;

  const steps = raw.steps.filter(isTraceStep);
  // An envelope whose steps are all malformed carries nothing renderable. An envelope
  // with genuinely zero steps is different, and is handled by the panel as an empty state.
  if (steps.length === 0 && raw.steps.length > 0) return null;

  const totals = raw.total_tokens;
  return {
    thread_id: typeof raw.thread_id === 'string' ? raw.thread_id : '',
    trace_id: typeof raw.trace_id === 'string' ? raw.trace_id : '',
    created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    total_duration:
      typeof raw.total_duration === 'number' && Number.isFinite(raw.total_duration)
        ? raw.total_duration
        : sumDuration(steps),
    total_tokens: isRecord(totals)
      ? {
          in: Number(totals.in) || 0,
          out: Number(totals.out) || 0,
          total: Number(totals.total) || 0,
          cost: typeof totals.cost === 'number' ? totals.cost : null,
        }
      : sumTokens(steps),
    steps,
  };
};

/**
 * Build an envelope from a bare `trace_events` list.
 *
 * `ChatResponse` carries both `trace_envelope` and `trace_events` — the same steps, with
 * and without the header (a deliberate Commit 8 decision, not redundancy). If envelope
 * assembly failed server-side but the events survived, the panel still has everything it
 * needs; the header is just recomputed here instead of read.
 */
export const envelopeFromSteps = (
  raw: unknown,
  meta: { thread_id?: string; trace_id?: string; created_at?: string },
): TraceEnvelope | null => {
  if (!Array.isArray(raw)) return null;
  const steps = raw.filter(isTraceStep);
  if (steps.length === 0) return null;
  return {
    thread_id: meta.thread_id ?? '',
    trace_id: meta.trace_id ?? '',
    created_at: meta.created_at ?? '',
    total_duration: sumDuration(steps),
    total_tokens: sumTokens(steps),
    steps,
  };
};
