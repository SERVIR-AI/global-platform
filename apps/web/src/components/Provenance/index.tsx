import { resolveApiUrl } from '@/lib/api';
import { latestBrief, parseProvenance, type ProvenanceData } from '@/lib/provenance';
import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import type { Citation } from '@/types/chat';
import {
  Archive,
  Bot,
  CalendarCog,
  ExternalLink,
  FileText,
  Radio,
  ShieldCheck,
  Waypoints,
} from 'lucide-react';
import { FC, useLayoutEffect, useMemo, useRef, useState } from 'react';

/**
 * The insight-provenance graph, step by step: question -> parse (model) ->
 * feeds (the literal retrieval queries / live conditions query / calendar) ->
 * evidence chunks grouped under their documents -> the synthesis + groundedness
 * gate -> claims. Model steps are dashed and badged; deterministic steps are
 * solid — the distinction is the point. Hover any node to trace its full path.
 */

const SECTION_EDGE = ['#38bdf8', '#34d399', '#fbbf24', '#a78bfa'];
const SECTION_BORDER = [
  'border-sky-400',
  'border-emerald-400',
  'border-amber-400',
  'border-violet-400',
];
const KIND_ICON = { document: FileText, conditions: Radio, calendar: CalendarCog } as const;

interface Edge {
  from: string;
  to: string;
  color: string;
  d?: string;
}

const ModelBadge: FC = () => (
  <span className="badge badge-xs gap-0.5 border-violet-400 text-violet-500 bg-transparent">
    <Bot className="w-2.5 h-2.5" />
    model step
  </span>
);

const ChunkNode: FC<{ citation: Citation; lit: boolean; register: (el: HTMLElement | null) => void }> = ({
  citation: c,
  lit,
  register,
}) => (
  <div
    ref={register}
    className={cn(
      'rounded border bg-base-100 px-1.5 py-1 text-[0.65rem] leading-snug transition-opacity',
      lit ? 'border-primary/60' : 'border-base-300',
      !lit && 'opacity-30',
    )}
  >
    <div className="flex items-center gap-1">
      <span className="font-mono font-bold text-primary">[{c.n}]</span>
      {typeof c.score === 'number' && (
        <span className="text-base-content/40">relevance {c.score.toFixed(2)}</span>
      )}
      {c.chunk_id && <span className="text-base-content/30 truncate">{c.chunk_id}</span>}
    </div>
    {c.text && (
      <div className="text-base-content/60">
        {c.text.length > 110 ? `${c.text.slice(0, 110)}…` : c.text}
      </div>
    )}
  </div>
);

const Graph: FC<{ data: ProvenanceData }> = ({ data }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef(new Map<string, HTMLElement>());
  const [paths, setPaths] = useState<Edge[]>([]);
  const [canvas, setCanvas] = useState({ w: 0, h: 0 });
  const [hover, setHover] = useState<string | null>(null);

  const register = (id: string) => (el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(id, el);
    else nodeRefs.current.delete(id);
  };

  // Full edge list (including the pipeline lane, for hover traversal); only
  // cross-column edges get SVG paths — the lane is drawn as a vertical connector.
  const edges = useMemo<Edge[]>(() => {
    const list: Edge[] = [{ from: 'question', to: 'parse', color: '#a78bfa' }];
    for (const f of data.feeds) list.push({ from: 'parse', to: f.id, color: '#a78bfa' });
    for (const f of data.feeds)
      for (const n of f.cites) list.push({ from: f.id, to: `cite-${n}`, color: '#94a3b8' });
    for (const claim of data.claims)
      for (const n of claim.cites)
        list.push({
          from: `cite-${n}`,
          to: claim.id,
          color: SECTION_EDGE[claim.section % SECTION_EDGE.length],
        });
    return list;
  }, [data]);

  useLayoutEffect(() => {
    const measure = () => {
      const container = containerRef.current;
      if (!container) return;
      const base = container.getBoundingClientRect();
      const next: Edge[] = [];
      for (const e of edges) {
        const from = nodeRefs.current.get(e.from)?.getBoundingClientRect();
        const to = nodeRefs.current.get(e.to)?.getBoundingClientRect();
        if (!from || !to) continue;
        if (to.left - from.right < 8) continue; // lane edges: drawn by the connector, not SVG
        const x1 = from.right - base.left + container.scrollLeft;
        const y1 = from.top + from.height / 2 - base.top + container.scrollTop;
        const x2 = to.left - base.left + container.scrollLeft;
        const y2 = to.top + to.height / 2 - base.top + container.scrollTop;
        const mid = (x1 + x2) / 2;
        next.push({ ...e, d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}` });
      }
      setPaths(next);
      // Size the SVG to the CONTENT, not the visible client box — otherwise
      // edges past the first fold are clipped.
      setCanvas({ w: container.scrollWidth, h: container.scrollHeight });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (containerRef.current) ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, [edges]);

  // Hover tracing: walk the graph up- and downstream from the hovered node,
  // collecting only the edges actually traversed — the node's full path.
  const { litNodes, litEdges } = useMemo(() => {
    const nodes = new Set<string>();
    const lit = new Set<string>();
    if (!hover) return { litNodes: nodes, litEdges: lit };
    const key = (e: Edge) => `${e.from}->${e.to}`;
    const walk = (start: string, dir: 'up' | 'down') => {
      const stack = [start];
      while (stack.length) {
        const at = stack.pop()!;
        for (const e of edges) {
          const [next, hit] = dir === 'down' ? [e.to, e.from === at] : [e.from, e.to === at];
          if (!hit || lit.has(key(e))) continue;
          lit.add(key(e));
          nodes.add(next);
          stack.push(next);
        }
      }
    };
    nodes.add(hover);
    walk(hover, 'down');
    walk(hover, 'up');
    return { litNodes: nodes, litEdges: lit };
  }, [hover, edges]);

  const active = (id: string) => !hover || litNodes.has(id);
  const hoverProps = (id: string) => ({
    onMouseEnter: () => setHover(id),
    onMouseLeave: () => setHover(null),
  });
  const usedCites = new Set(data.claims.flatMap((c) => c.cites));

  return (
    <div ref={containerRef} className="relative grow min-h-0 overflow-auto p-3">
      <svg
        className="absolute top-0 left-0 pointer-events-none"
        width={canvas.w}
        height={canvas.h}
      >
        {paths.map((e, i) => {
          const lit = !hover || litEdges.has(`${e.from}->${e.to}`);
          return (
            <path
              key={i}
              d={e.d}
              fill="none"
              stroke={e.color}
              strokeWidth={lit && hover ? 2.5 : 1.5}
              opacity={lit ? (hover ? 0.9 : 0.4) : 0.06}
            />
          );
        })}
      </svg>
      <div className="relative flex gap-8 items-start">
        {/* Pipeline lane: question -> parse -> feeds (vertical connector) */}
        <div className="w-52 shrink-0 flex flex-col gap-2 relative">
          <div className="absolute left-3 top-4 bottom-4 w-px bg-base-300" />
          <div
            ref={register('question')}
            {...hoverProps('question')}
            className={cn(
              'relative rounded-lg border border-base-300 bg-base-200 p-2 text-[0.7rem] italic transition-opacity',
              !active('question') && 'opacity-30',
            )}
          >
            “{data.question}”
          </div>
          <div
            ref={register('parse')}
            {...hoverProps('parse')}
            className={cn(
              'relative rounded-lg border border-dashed border-violet-400 bg-base-100 p-2 text-[0.7rem] transition-opacity',
              !active('parse') && 'opacity-30',
            )}
          >
            <div className="flex items-center justify-between gap-1 font-semibold">
              parse the question
              <ModelBadge />
            </div>
            {data.parsed && (
              <div className="mt-0.5 text-base-content/60">
                crop: {data.parsed.crop || '—'} · country: {data.parsed.country || '—'}
                <div className="truncate">focus: {data.parsed.focus || '—'}</div>
              </div>
            )}
          </div>
          {data.feeds.map((f) => (
            <div
              key={f.id}
              ref={register(f.id)}
              {...hoverProps(f.id)}
              className={cn(
                'relative rounded-lg border bg-base-100 p-2 text-[0.7rem] transition-opacity',
                f.empty ? 'border-dashed border-warning/60' : 'border-base-300',
                !active(f.id) && 'opacity-30',
              )}
            >
              <div className="font-semibold">{f.label}</div>
              {f.detail && (
                <div className="mt-0.5 font-mono text-[0.6rem] text-base-content/50 break-words">
                  {f.detail}
                </div>
              )}
              <div className={cn('text-[0.6rem]', f.empty ? 'text-warning' : 'text-base-content/40')}>
                {f.empty ? '0 chunks matched — declared as a gap' : `${f.cites.length} evidence entr${f.cites.length === 1 ? 'y' : 'ies'}`}
              </div>
            </div>
          ))}
        </div>

        {/* Evidence: chunks grouped under their documents */}
        <div className="w-72 shrink-0 flex flex-col gap-2">
          <div className="text-[0.65rem] font-semibold uppercase tracking-wide text-base-content/50">
            Evidence — chunks, inside their documents
          </div>
          {data.docs.map((doc) => {
            const Icon = KIND_ICON[doc.kind] ?? FileText;
            return (
              <div key={doc.key} className="rounded-lg border border-base-300 bg-base-200/60 p-1.5">
                <div className="flex items-center gap-1.5 text-[0.68rem] font-semibold px-0.5">
                  <Icon className="w-3 h-3 shrink-0" />
                  <span className="truncate">{doc.source}</span>
                  {doc.kind === 'calendar' && (
                    <span className={cn('badge badge-xs', doc.adjusted ? 'badge-warning' : 'badge-ghost')}>
                      {doc.adjusted ? 'adjusted' : 'default'}
                    </span>
                  )}
                  <span className="grow" />
                  {doc.url && (
                    <a href={doc.url} target="_blank" rel="noreferrer" className="link" title="Open the original">
                      <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
                  {doc.archived_copy && (
                    <a
                      href={resolveApiUrl(doc.archived_copy)}
                      target="_blank"
                      rel="noreferrer"
                      className="link"
                      title="Our archived copy — the exact document this system read"
                    >
                      <Archive className="w-3 h-3" />
                    </a>
                  )}
                </div>
                {doc.title && (
                  <div className="px-0.5 text-[0.6rem] text-base-content/50 truncate">{doc.title}</div>
                )}
                <div className="mt-1 flex flex-col gap-1">
                  {doc.citations.map((c) => (
                    <div key={c.n} {...hoverProps(`cite-${c.n}`)}>
                      <ChunkNode citation={c} lit={active(`cite-${c.n}`)} register={register(`cite-${c.n}`)} />
                      {!usedCites.has(c.n) && (
                        <div className="text-[0.58rem] text-base-content/40 px-1">
                          retrieved, not cited by any claim
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>

        {/* The synthesis + groundedness gate */}
        <div className="w-36 shrink-0 self-stretch flex items-center">
          <div className="rounded-lg border border-dashed border-violet-400 bg-base-100 p-2 text-[0.65rem]">
            <div className="flex items-center justify-between gap-1 font-semibold">
              synthesis
              <ModelBadge />
            </div>
            <div className="text-base-content/60 break-words">{data.model}</div>
            <div className="mt-1.5 flex items-center gap-1 font-semibold">
              <ShieldCheck className="w-3 h-3 text-success" />
              groundedness
            </div>
            <div className="text-base-content/60">
              {data.grounded?.passed
                ? `passed, attempt ${data.grounded.attempts ?? 1}`
                : 'not available'}
            </div>
            <div className="text-[0.58rem] text-base-content/40 mt-1">
              every claim to the right survived this gate
            </div>
          </div>
        </div>

        {/* Claims, grouped by brief section */}
        <div className="flex-1 min-w-56 flex flex-col gap-3">
          {data.sections.map((title, si) => {
            const claims = data.claims.filter((c) => c.section === si);
            if (claims.length === 0) return null;
            return (
              <div key={title} className="flex flex-col gap-1.5">
                <div
                  className="text-[0.65rem] font-semibold uppercase tracking-wide"
                  style={{ color: SECTION_EDGE[si % SECTION_EDGE.length] }}
                >
                  {title}
                </div>
                {claims.map((claim) => (
                  <div
                    key={claim.id}
                    ref={register(claim.id)}
                    {...hoverProps(claim.id)}
                    className={cn(
                      'rounded-lg border-l-4 border border-base-300 bg-base-100 p-2 text-[0.7rem] leading-snug transition-opacity cursor-default',
                      SECTION_BORDER[claim.section % SECTION_BORDER.length],
                      !active(claim.id) && 'opacity-30',
                    )}
                  >
                    {claim.text.length > 200 ? `${claim.text.slice(0, 200)}…` : claim.text}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

const ProvenancePanel: FC = () => {
  const messages = useChatStore((s) => s.messages);
  const latest = useMemo(() => latestBrief(messages), [messages]);
  const data = useMemo(
    () => (latest ? parseProvenance(latest.item, latest.question) : null),
    [latest],
  );

  return (
    <div className="basis-1 grow flex flex-col min-h-0 border-l border-base-300">
      <div className="px-3 py-2 border-b border-base-300 flex items-center gap-2">
        <Waypoints className="w-4 h-4" />
        <span className="text-sm font-semibold">Insight provenance</span>
        <span className="text-xs text-base-content/50">
          question → parse → feeds → evidence → checks → claims. Hover to trace a path.
        </span>
      </div>
      {data ? (
        <Graph data={data} />
      ) : (
        <div className="grow flex items-center justify-center p-8 text-center text-sm text-base-content/50">
          Ask a food-security question — the full construction path of the answer, from your
          question through retrieval and checks to every claim, will render here.
        </div>
      )}
    </div>
  );
};

export default ProvenancePanel;
