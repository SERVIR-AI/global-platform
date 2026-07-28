import { NODE_LABEL } from '@/lib/trace/labels';
import { formatDuration, type TraceStepRow } from '@/lib/trace/selectors';
import { cn } from '@/lib/utils';
import type { TraceNode } from '@/types/trace';
import { AlertTriangle, PauseCircle } from 'lucide-react';
import { FC } from 'react';

type TraceStepsProps = {
  rows: TraceStepRow[];
  selectedIndex: number | null;
  onSelect: (index: number) => void;
};

const STATUS_BAR: Record<TraceStepRow['status'], string> = {
  ok: 'bg-primary',
  error: 'bg-error',
  paused: 'bg-warning',
};

/**
 * Tier 2 — one row per step, in execution order.
 *
 * The bar is a proportional share of the turn's total duration, not a position on a
 * timeline. LangGraph runs these nodes in series (`graph.py`'s edges are all sequential),
 * so a true waterfall would imply an overlap that never happens.
 */
const TraceSteps: FC<TraceStepsProps> = ({ rows, selectedIndex, onSelect }) => (
  <ul className="flex flex-col gap-1">
    {rows.map((row) => (
      <li key={row.index}>
        <button
          type="button"
          onClick={() => onSelect(row.index)}
          aria-expanded={selectedIndex === row.index}
          className={cn(
            'w-full text-left rounded-lg px-2 py-1.5 transition-colors',
            'hover:bg-base-200 focus-visible:outline focus-visible:outline-2',
            selectedIndex === row.index && 'bg-base-200',
          )}
        >
          <div className="flex items-center gap-2">
            <span className="badge badge-xs badge-ghost shrink-0">
              {NODE_LABEL[row.node as TraceNode] ?? row.node}
            </span>
            <span className="font-medium grow min-w-0 truncate">{row.title}</span>
            {row.status === 'error' && (
              <AlertTriangle className="w-3.5 h-3.5 text-error shrink-0" aria-label="Problem" />
            )}
            {row.status === 'paused' && (
              <PauseCircle className="w-3.5 h-3.5 text-warning shrink-0" aria-label="Paused" />
            )}
            <span className="text-base-content/50 shrink-0 tabular-nums">
              {formatDuration(row.durationMs)}
            </span>
          </div>

          {/* Backend-authored summary — rendered verbatim, never paraphrased. */}
          <div className="text-base-content/70 mt-0.5 break-words">{row.summary}</div>

          {row.error && <div className="text-error mt-0.5 break-words">{row.error}</div>}

          <div className="mt-1 h-1 w-full rounded-full bg-base-300 overflow-hidden">
            <div
              className={cn('h-full rounded-full', STATUS_BAR[row.status])}
              // A zero-width bar is invisible and reads as a rendering bug, so anything
              // that ran at all keeps a 2% floor.
              style={{ width: `${Math.max(2, row.durationFraction * 100)}%` }}
            />
          </div>
        </button>
      </li>
    ))}
  </ul>
);

export default TraceSteps;
