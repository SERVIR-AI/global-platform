/**
 * The backend LangGraph state machine, as drawable data.
 *
 * Transcribed by hand from `apps/api/src/app/graph/graph.py:487-500` (`_build_graph`) and
 * its three routing functions, `_after_route` / `_after_resolve` / `_after_fetch`.
 *
 * Coordinates live here, not in the component, so re-laying-out the diagram is an edit to
 * this table rather than to rendering code. Everything is in the SVG user space defined by
 * VIEW_BOX; the spine runs down x=150 and conditional bypasses bracket out to the left, so
 * the right-hand side stays free for the paused branch.
 *
 * This is a hardcoded copy of a structure that lives in Python, which is a real drift risk.
 * The guard is `test_graph_topology_matches_frontend` in `apps/api/tests/test_tracing.py`:
 * it fails the moment `graph.py` gains or loses a node.
 */

export type GraphNodeId =
  | 'start'
  | 'route'
  | 'resolve'
  | 'fetch'
  | 'operate'
  | 'finalize'
  | 'end'
  | 'ask_end';

export interface GraphNodeShape {
  id: GraphNodeId;
  /** `terminal` nodes are the small pills (START / END); `node` are the graph's real nodes. */
  kind: 'node' | 'terminal';
  /** Center x/y and box size, in VIEW_BOX user units. */
  cx: number;
  cy: number;
  width: number;
  height: number;
}

export interface GraphEdgeShape {
  id: string;
  from: GraphNodeId;
  to: GraphNodeId;
  /** Plain-English condition, from the matching `_after_*` branch. Shown on hover. */
  when: string;
  /** Precomputed SVG path. Straight lines on the spine, beziers for the bypasses. */
  path: string;
}

export const VIEW_BOX = '0 0 344 462';

export const GRAPH_NODES: GraphNodeShape[] = [
  { id: 'start', kind: 'terminal', cx: 150, cy: 18, width: 64, height: 22 },
  { id: 'route', kind: 'node', cx: 150, cy: 68, width: 120, height: 34 },
  { id: 'resolve', kind: 'node', cx: 150, cy: 144, width: 120, height: 34 },
  { id: 'fetch', kind: 'node', cx: 150, cy: 220, width: 120, height: 34 },
  { id: 'operate', kind: 'node', cx: 150, cy: 296, width: 120, height: 34 },
  { id: 'finalize', kind: 'node', cx: 150, cy: 372, width: 120, height: 34 },
  { id: 'end', kind: 'terminal', cx: 150, cy: 438, width: 64, height: 22 },
  { id: 'ask_end', kind: 'terminal', cx: 290, cy: 144, width: 96, height: 26 },
];

/**
 * Every edge the graph can take. Nine come from `_build_graph`; `start->route` and
 * `finalize->end` are the two unconditional ones (`add_edge(START, "route")`,
 * `add_edge("finalize", END)`).
 *
 * `resolve->ask_end` is the human-in-the-loop pause: `_after_resolve` returns `"ask_end"`,
 * which `add_conditional_edges` maps to END. The turn stops there and the next turn
 * resumes at `route` with `kind: "apply_choice"`.
 */
export const GRAPH_EDGES: GraphEdgeShape[] = [
  {
    id: 'start->route',
    from: 'start',
    to: 'route',
    when: 'every turn starts here',
    path: 'M 150 29 L 150 51',
  },
  {
    id: 'route->resolve',
    from: 'route',
    to: 'resolve',
    when: 'normal turn',
    path: 'M 150 85 L 150 127',
  },
  {
    id: 'route->fetch',
    from: 'route',
    to: 'fetch',
    when: 'resumed a choice you already made',
    path: 'M 90 76 C 56 76, 56 212, 90 212',
  },
  {
    id: 'route->finalize',
    from: 'route',
    to: 'finalize',
    when: 'could not understand the question',
    path: 'M 90 60 C 14 60, 14 364, 90 364',
  },
  {
    id: 'resolve->fetch',
    from: 'resolve',
    to: 'fetch',
    when: 'nothing to choose between',
    path: 'M 150 161 L 150 203',
  },
  {
    id: 'resolve->ask_end',
    from: 'resolve',
    to: 'ask_end',
    when: 'paused to ask you which kind of answer you want',
    path: 'M 210 144 L 242 144',
  },
  {
    id: 'resolve->finalize',
    from: 'resolve',
    to: 'finalize',
    when: 'no data available for that hazard',
    path: 'M 90 152 C 35 152, 35 372, 90 372',
  },
  {
    id: 'fetch->operate',
    from: 'fetch',
    to: 'operate',
    when: 'data gathered successfully',
    path: 'M 150 237 L 150 279',
  },
  {
    id: 'fetch->finalize',
    from: 'fetch',
    to: 'finalize',
    when: 'could not gather the data',
    path: 'M 90 228 C 56 228, 56 380, 90 380',
  },
  {
    id: 'operate->finalize',
    from: 'operate',
    to: 'finalize',
    when: 'number computed',
    path: 'M 150 313 L 150 355',
  },
  {
    id: 'finalize->end',
    from: 'finalize',
    to: 'end',
    when: 'answer returned to you',
    path: 'M 150 389 L 150 427',
  },
];

/** The five real graph nodes, in the order the drift test compares them. */
export const GRAPH_NODE_IDS = ['route', 'resolve', 'fetch', 'operate', 'finalize'] as const;
