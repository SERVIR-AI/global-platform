import type { FieldAudience } from '@/lib/trace/fields';
import { NODE_ID_BY_STEP, toGraphPath } from '@/lib/trace/graphPath';
import type { GraphNodeId } from '@/lib/trace/graphTopology';
import { stepTitle } from '@/lib/trace/labels';
import { envelopeFromSteps, parseEnvelope } from '@/lib/trace/parse';
import { summarizeEnvelope, toStepRows } from '@/lib/trace/selectors';
import { cn } from '@/lib/utils';
import type { ChatItem } from '@/types/chat';
import type { TraceNode } from '@/types/trace';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { FC, useMemo, useState } from 'react';
import TraceErrorBoundary from './TraceErrorBoundary';
import TraceGraph from './TraceGraph';
import TraceStepDetail from './TraceStepDetail';
import TraceSteps from './TraceSteps';
import TraceSummary from './TraceSummary';

type TracePanelProps = {
  item: ChatItem;
};

/**
 * Resolve a turn's trace, preferring the server-assembled envelope.
 *
 * `ChatResponse` carries `trace_envelope` AND `trace_events` — the same steps with and
 * without the computed header. That is deliberate, and it means
 * a turn whose envelope assembly failed server-side can still be rendered from the bare
 * event list with the header recomputed client-side.
 */
const resolveEnvelope = (item: ChatItem) => {
  if (!('id' in item)) return null; // a request turn has no trace of its own
  const fromEnvelope = parseEnvelope(item.trace_envelope);
  if (fromEnvelope) return fromEnvelope;
  return envelopeFromSteps(item.trace_events, {
    thread_id: item.thread_id,
    trace_id: item.id,
    created_at: item.created_at,
  });
};

const TracePanelInner: FC<TracePanelProps> = ({ item }) => {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<FieldAudience>('user');
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);

  const envelope = useMemo(() => resolveEnvelope(item), [item]);
  const summary = useMemo(() => (envelope ? summarizeEnvelope(envelope) : null), [envelope]);
  const rows = useMemo(() => (envelope ? toStepRows(envelope) : []), [envelope]);
  const graphPath = useMemo(() => (envelope ? toGraphPath(envelope) : null), [envelope]);

  // No trace at all is the normal case for a request turn, an error turn built client-side
  // in useChat, or a turn whose envelope build failed. Render nothing rather than an empty
  // shell — the answer stands on its own.
  if (!envelope || !summary || !graphPath) return null;

  const selected = selectedIndex === null ? null : rows[selectedIndex];
  const selectedNode: GraphNodeId | null = selected
    ? (NODE_ID_BY_STEP[selected.node as TraceNode] ?? null)
    : null;

  // Clicking a node in the diagram opens the same detail the step row would.
  const selectByNode = (nodeId: GraphNodeId) => {
    const index = rows.findIndex((row) => NODE_ID_BY_STEP[row.node as TraceNode] === nodeId);
    if (index >= 0) setSelectedIndex(index === selectedIndex ? null : index);
  };

  return (
    <div className="text-xs text-base-content/80 max-w-2xl w-full">
      <button
        type="button"
        className="flex items-start gap-1 text-left w-full hover:text-base-content"
        onClick={() => setOpen((wasOpen) => !wasOpen)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="w-3.5 h-3.5 mt-0.5 shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 mt-0.5 shrink-0" />
        )}
        <TraceSummary summary={summary} detail={detail} />
      </button>

      {open && (
        <div className="mt-2 flex flex-col gap-3">
          {rows.length === 0 ? (
            <p className="text-base-content/50">No steps were recorded for this answer.</p>
          ) : (
            <>
              <div className="flex flex-col lg:flex-row gap-4">
                <div className="lg:w-[344px] shrink-0">
                  <TraceGraph
                    path={graphPath}
                    selectedNode={selectedNode}
                    onSelectNode={selectByNode}
                  />
                  <p className="text-base-content/40 mt-1">
                    Dashed steps weren&apos;t needed for this question.
                  </p>
                </div>
                <div className="grow min-w-0">
                  <TraceSteps
                    rows={rows}
                    selectedIndex={selectedIndex}
                    onSelect={(index) => setSelectedIndex(index === selectedIndex ? null : index)}
                  />
                </div>
              </div>

              {selected && (
                <TraceStepDetail
                  step={selected.step}
                  title={stepTitle(selected.step)}
                  detail={detail}
                />
              )}
            </>
          )}

          <label className="flex items-center gap-2 cursor-pointer self-start text-base-content/50">
            <input
              type="checkbox"
              className={cn('toggle toggle-xs')}
              checked={detail === 'developer'}
              onChange={(event) => setDetail(event.target.checked ? 'developer' : 'user')}
            />
            Show technical detail
          </label>
        </div>
      )}
    </div>
  );
};

/**
 * Tiers 1–3 of the per-turn trace, wrapped so it can never break the bubble it sits in.
 */
const TracePanel: FC<TracePanelProps> = ({ item }) => (
  <TraceErrorBoundary>
    <TracePanelInner item={item} />
  </TraceErrorBoundary>
);

export default TracePanel;
