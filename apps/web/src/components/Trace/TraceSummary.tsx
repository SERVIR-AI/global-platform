import {
  formatCost,
  formatDuration,
  formatTokens,
  type TraceSummaryView,
  type TurnOutcome,
} from '@/lib/trace/selectors';
import type { FieldAudience } from '@/lib/trace/fields';
import { failedAt } from '@/lib/trace/labels';
import { BadgeCheck, CircleAlert, PauseCircle } from 'lucide-react';
import { FC } from 'react';

type TraceSummaryProps = {
  summary: TraceSummaryView;
  detail: FieldAudience;
};

const OUTCOME: Record<TurnOutcome, { label: string; className: string }> = {
  answered: { label: 'Answered', className: 'text-base-content/60' },
  paused: { label: 'Waiting on you', className: 'text-warning' },
  failed: { label: 'Hit a problem', className: 'text-error' },
};

/**
 * Tier 1 — the always-visible line.
 *
 * Duration and step count are for everyone. Tokens and cost are developer detail and only
 * appear with the toggle on, because a token count answers no question an analyst has.
 * Groundedness is the exception in the other direction: it is the most trust-relevant
 * field in the whole envelope, so it earns a place in the collapsed header.
 */
const TraceSummary: FC<TraceSummaryProps> = ({ summary, detail }) => {
  const outcome = OUTCOME[summary.outcome];

  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <span className="font-medium">How this answer was produced</span>
      <span className="text-base-content/50">
        {summary.stepCount} {summary.stepCount === 1 ? 'step' : 'steps'} ·{' '}
        {formatDuration(summary.totalDurationMs)}
      </span>

      <span className={outcome.className}>
        {summary.outcome === 'paused' && (
          <PauseCircle className="inline w-3.5 h-3.5 mr-1 align-[-2px]" />
        )}
        {summary.outcome === 'failed' && (
          <CircleAlert className="inline w-3.5 h-3.5 mr-1 align-[-2px]" />
        )}
        {outcome.label}
        {summary.failedNode !== null && ` ${failedAt(summary.failedNode)}`}
      </span>

      {summary.grounded === true && (
        <span className="text-success" title="The calculated number appears in the answer text.">
          <BadgeCheck className="inline w-3.5 h-3.5 mr-1 align-[-2px]" />
          Backed by the number
        </span>
      )}
      {summary.grounded === false && (
        <span
          className="text-warning"
          title="The calculated number does not appear verbatim in the answer text."
        >
          <CircleAlert className="inline w-3.5 h-3.5 mr-1 align-[-2px]" />
          Number not quoted
        </span>
      )}

      {detail === 'developer' && (
        <span className="text-base-content/50">
          {formatTokens(summary.tokensTotal)} tokens · {formatCost(summary.costUsd)}
        </span>
      )}
    </span>
  );
};

export default TraceSummary;
