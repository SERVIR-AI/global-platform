import type { GraphNodeState, GraphPath } from '@/lib/trace/graphPath';
import { GRAPH_EDGES, GRAPH_NODES, VIEW_BOX, type GraphNodeId } from '@/lib/trace/graphTopology';
import {
  GRAPH_EDGE_KEY,
  GRAPH_KEY_LABEL,
  GRAPH_NODE_LABEL,
  NODE_STATE_LABEL,
} from '@/lib/trace/labels';
import { cn } from '@/lib/utils';
import { FC, useId } from 'react';

type TraceGraphProps = {
  path: GraphPath;
  selectedNode?: GraphNodeId | null;
  onSelectNode?: (nodeId: GraphNodeId) => void;
};

/** Box styling per state. Semantic DaisyUI colors only — no hardcoded hex. */
const NODE_BOX: Record<GraphNodeState, string> = {
  visited: 'fill-primary/15 stroke-primary',
  skipped: 'fill-base-100 stroke-base-content/20 [stroke-dasharray:3_3]',
  errored: 'fill-error/15 stroke-error',
  paused: 'fill-warning/20 stroke-warning',
};

const NODE_TEXT: Record<GraphNodeState, string> = {
  visited: 'fill-base-content',
  skipped: 'fill-base-content/35',
  errored: 'fill-base-content',
  paused: 'fill-base-content',
};

/**
 * Marks for the states that need one, so the diagram survives greyscale, a colour-vision
 * difference, and a printed page.
 */
const GLYPH: Record<GraphNodeState, string> = {
  visited: '',
  skipped: '',
  errored: '✕',
  paused: '◼',
};

/** Fixed order for the key, so it does not reshuffle between turns. */
const STATE_ORDER: GraphNodeState[] = ['visited', 'skipped', 'paused', 'errored'];

/**
 * The key to the diagram above it.
 *
 * Only states actually on screen are listed: explaining a red box on a turn that has none
 * is noise, and the order is fixed so the entries never reshuffle between turns.
 */
const GraphKey: FC<{ path: GraphPath }> = ({ path }) => {
  const states = Object.values(path.nodeStates);
  const present = STATE_ORDER.filter((state) => states.includes(state));

  return (
    <figcaption className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[0.65rem] text-base-content/60">
      {present.map((state) => (
        <span key={state} className="inline-flex items-center gap-1">
          <svg width="14" height="10" aria-hidden className="shrink-0 overflow-visible">
            <rect
              x="0.75"
              y="0.75"
              width="12.5"
              height="8.5"
              rx="2"
              strokeWidth="1"
              className={NODE_BOX[state]}
            />
          </svg>
          {GLYPH[state] ? <span aria-hidden>{GLYPH[state]}</span> : null}
          {GRAPH_KEY_LABEL[state]}
        </span>
      ))}

      <span className="inline-flex items-center gap-1">
        <svg width="16" height="10" aria-hidden className="shrink-0">
          <line x1="1" y1="5" x2="15" y2="5" strokeWidth="2" className="stroke-primary" />
        </svg>
        {GRAPH_EDGE_KEY.taken}
      </span>
      <span className="inline-flex items-center gap-1">
        <svg width="16" height="10" aria-hidden className="shrink-0">
          <line
            x1="1"
            y1="5"
            x2="15"
            y2="5"
            strokeWidth="1"
            className="stroke-base-content/20 [stroke-dasharray:3_3]"
          />
        </svg>
        {GRAPH_EDGE_KEY.idle}
      </span>
    </figcaption>
  );
};

/**
 * The backend graph, with this turn's path lit up.
 *
 * Topology and coordinates come from `graphTopology.ts`; which parts are lit comes from
 * `graphPath.ts`. This component only draws — so a different diagram (horizontal, radial,
 * animated) is a rewrite of this file alone.
 *
 * Skipped nodes are drawn, not hidden. "It never needed to ask you anything" is as
 * informative as "it did", and only visible if the unused branches are on screen.
 */
const TraceGraph: FC<TraceGraphProps> = ({ path, selectedNode, onSelectNode }) => {
  // Marker ids are document-global, so every panel on the page needs its own namespace.
  const uid = useId().replace(/:/g, '');
  const arrowTaken = `${uid}-arrow-taken`;
  const arrowIdle = `${uid}-arrow-idle`;

  return (
    <figure className="m-0">
      <svg
        viewBox={VIEW_BOX}
        className="w-full max-w-[344px] h-auto"
        role="img"
        aria-label="Diagram of the steps the system can take, with this answer's path highlighted"
      >
        <defs>
          <marker
            id={arrowTaken}
            viewBox="0 0 8 8"
            refX="6"
            refY="4"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path d="M 0 1 L 7 4 L 0 7 z" className="fill-primary" />
          </marker>
          <marker
            id={arrowIdle}
            viewBox="0 0 8 8"
            refX="6"
            refY="4"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path d="M 0 1 L 7 4 L 0 7 z" className="fill-base-content/20" />
          </marker>
        </defs>

        {GRAPH_EDGES.map((edge) => {
          const taken = path.edgesTaken.has(edge.id);
          return (
            <path
              key={edge.id}
              d={edge.path}
              fill="none"
              strokeWidth={taken ? 2 : 1}
              markerEnd={`url(#${taken ? arrowTaken : arrowIdle})`}
              className={cn(
                taken ? 'stroke-primary' : 'stroke-base-content/20 [stroke-dasharray:3_3]',
                // The pause exit is the one edge that means "stopped on purpose", so it
                // reads as a warning rather than as part of the successful path.
                taken && edge.id === 'resolve->ask_end' && 'stroke-warning',
              )}
            >
              <title>{edge.when}</title>
            </path>
          );
        })}

        {GRAPH_NODES.map((node) => {
          const state = path.nodeStates[node.id];
          const label = GRAPH_NODE_LABEL[node.id] ?? node.id;
          const interactive = node.kind === 'node' && state !== 'skipped' && Boolean(onSelectNode);
          return (
            <g
              key={node.id}
              className={interactive ? 'cursor-pointer' : undefined}
              onClick={interactive ? () => onSelectNode?.(node.id) : undefined}
            >
              <title>{`${label} — ${NODE_STATE_LABEL[state]}`}</title>
              <rect
                x={node.cx - node.width / 2}
                y={node.cy - node.height / 2}
                width={node.width}
                height={node.height}
                rx={node.kind === 'terminal' ? node.height / 2 : 6}
                strokeWidth={selectedNode === node.id ? 2.5 : 1.25}
                className={cn(NODE_BOX[state], selectedNode === node.id && 'stroke-[2.5]')}
              />
              <text
                x={node.cx}
                y={node.cy}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={node.kind === 'terminal' ? 9 : 11}
                className={cn(NODE_TEXT[state], 'select-none')}
              >
                {node.kind === 'node' && GLYPH[state] ? `${GLYPH[state]} ${label}` : label}
              </text>
            </g>
          );
        })}
      </svg>
      <GraphKey path={path} />
    </figure>
  );
};

export default TraceGraph;
