/**
 * Which nodes ran, and which edges were taken, for one turn.
 *
 * The envelope records only the nodes that RAN, in order. The nodes that didn't are half
 * the information — "it never needed to ask you anything" is as meaningful as "it did" —
 * which is why the topology is a separate table rather than derived from the steps.
 */

import type { TraceEnvelope, TraceNode } from '@/types/trace';
import { GRAPH_EDGES, type GraphNodeId } from './graphTopology';

/**
 * The one place the `router` / `route` mismatch is reconciled.
 *
 * The trace tags route()'s step `node: "router"` (`tracing.py:144,212`, and
 *  `routeStep`), but the LangGraph node is registered as `"route"`
 * (`graph.py:488`). Every other node matches 1:1. Renaming either side is expensive and
 * cosmetic — envelopes already written to `cache/traces/` say `"router"`, and `"route"` is
 * what `_after_route`'s edge maps key off — so the two names stand and meet here.
 */
export const NODE_ID_BY_STEP: Record<TraceNode, GraphNodeId> = {
  router: 'route',
  resolve: 'resolve',
  fetch: 'fetch',
  operate: 'operate',
  finalize: 'finalize',
};

export type GraphNodeState = 'visited' | 'skipped' | 'errored' | 'paused';

export interface GraphPath {
  nodeStates: Record<GraphNodeId, GraphNodeState>;
  /** Ids of `GRAPH_EDGES` entries this turn traversed. */
  edgesTaken: Set<string>;
  /** True when the turn stopped at resolve() waiting on the user. */
  paused: boolean;
}

const EDGE_IDS = new Set(GRAPH_EDGES.map((edge) => edge.id));

const BASE_STATES: Record<GraphNodeId, GraphNodeState> = {
  start: 'skipped',
  route: 'skipped',
  resolve: 'skipped',
  fetch: 'skipped',
  operate: 'skipped',
  finalize: 'skipped',
  end: 'skipped',
  ask_end: 'skipped',
};

/**
 * Walk the step list into node states and traversed edges.
 *
 * Edges are derived by zipping consecutive visited nodes, which is sound because the graph
 * has no node reachable by two different edges from the same predecessor — every pair maps
 * to exactly one edge id. A pair with no matching edge (a topology this build predates) is
 * skipped rather than drawn, so the diagram degrades to "nodes only" instead of breaking.
 */
export const toGraphPath = (envelope: TraceEnvelope): GraphPath => {
  const nodeStates: Record<GraphNodeId, GraphNodeState> = { ...BASE_STATES };
  const edgesTaken = new Set<string>();

  const visited: GraphNodeId[] = [];
  for (const step of envelope.steps) {
    const id = NODE_ID_BY_STEP[step.node as TraceNode];
    if (!id) continue;
    visited.push(id);

    const paused = step.node === 'resolve' && step.awaiting_choice_set;
    const failed = 'error' in step && typeof step.error === 'string' && step.error.length > 0;
    nodeStates[id] = paused ? 'paused' : failed ? 'errored' : 'visited';
  }

  if (visited.length === 0) {
    return { nodeStates, edgesTaken, paused: false };
  }

  nodeStates.start = 'visited';
  edgesTaken.add(`start->${visited[0]}`);

  for (let i = 0; i < visited.length - 1; i += 1) {
    const id = `${visited[i]}->${visited[i + 1]}`;
    if (EDGE_IDS.has(id)) edgesTaken.add(id);
  }

  const last = envelope.steps[envelope.steps.length - 1];
  const paused = last.node === 'resolve' && last.awaiting_choice_set;

  if (paused) {
    // The turn ended at the pause, so the diagram must show the ask_end exit rather than
    // implying the run simply stopped mid-graph.
    nodeStates.ask_end = 'paused';
    edgesTaken.add('resolve->ask_end');
  } else if (last.node === 'finalize') {
    nodeStates.end = 'visited';
    edgesTaken.add('finalize->end');
  }

  return { nodeStates, edgesTaken, paused };
};
