import { resolveApiUrl } from '@/lib/api';
import { latestBrief, parseProvenance, type ProvenanceData } from '@/lib/provenance';
import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import type { Citation } from '@/types/chat';
import { Archive, CalendarCog, ExternalLink, FileText, Radio, Waypoints } from 'lucide-react';
import { FC, useLayoutEffect, useMemo, useRef, useState } from 'react';

/**
 * The provenance graph: the visual path from each claim in the brief back to
 * the sources that carry it. Renders in the map panel for food-security mode —
 * hover any node to trace its connections; source nodes link to the original
 * document and our archived copy.
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
  claimId: string;
  cite: number;
  section: number;
  d: string;
}

const SourceNode: FC<{ citation: Citation; hovered: boolean; dimmed: boolean }> = ({
  citation: c,
  hovered,
  dimmed,
}) => {
  const Icon = KIND_ICON[c.kind] ?? FileText;
  return (
    <div
      className={cn(
        'rounded-lg border bg-base-100 p-2 text-[0.7rem] transition-all',
        hovered ? 'border-primary shadow-md' : 'border-base-300',
        dimmed && 'opacity-30',
      )}
    >
      <div className="flex items-center gap-1.5 font-semibold">
        <Icon className="w-3 h-3 shrink-0" />
        <span className="font-mono text-primary">[{c.n}]</span>
        <span className="truncate">{c.source ?? 'unknown source'}</span>
      </div>
      <div className="mt-0.5 flex items-center gap-1.5 text-base-content/60">
        {c.title && <span className="truncate">{c.title}</span>}
        {c.pub_date && <span className="shrink-0">({c.pub_date})</span>}
      </div>
      <div className="mt-1 flex items-center gap-2">
        {c.validation && <span className="badge badge-ghost badge-xs">{c.validation}</span>}
        {c.kind === 'calendar' && (
          <span className={cn('badge badge-xs', c.adjusted ? 'badge-warning' : 'badge-ghost')}>
            {c.adjusted ? 'adjusted' : 'default'}
          </span>
        )}
        <span className="grow" />
        {c.url && (
          <a href={c.url} target="_blank" rel="noreferrer" className="link" title="Open the original">
            <ExternalLink className="w-3 h-3" />
          </a>
        )}
        {c.archived_copy && (
          <a
            href={resolveApiUrl(c.archived_copy)}
            target="_blank"
            rel="noreferrer"
            className="link"
            title="Open our archived copy — the exact document this system read"
          >
            <Archive className="w-3 h-3" />
          </a>
        )}
      </div>
    </div>
  );
};

const Graph: FC<{ data: ProvenanceData }> = ({ data }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef(new Map<string, HTMLElement>());
  const [edges, setEdges] = useState<Edge[]>([]);
  const [hover, setHover] = useState<string | null>(null); // claim id or `cite-n`

  const register = (id: string) => (el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(id, el);
    else nodeRefs.current.delete(id);
  };

  useLayoutEffect(() => {
    const measure = () => {
      const container = containerRef.current;
      if (!container) return;
      const base = container.getBoundingClientRect();
      const next: Edge[] = [];
      for (const claim of data.claims) {
        const from = nodeRefs.current.get(claim.id)?.getBoundingClientRect();
        if (!from) continue;
        for (const cite of claim.cites) {
          const to = nodeRefs.current.get(`cite-${cite}`)?.getBoundingClientRect();
          if (!to) continue;
          const x1 = from.right - base.left + container.scrollLeft;
          const y1 = from.top + from.height / 2 - base.top + container.scrollTop;
          const x2 = to.left - base.left + container.scrollLeft;
          const y2 = to.top + to.height / 2 - base.top + container.scrollTop;
          const mid = (x1 + x2) / 2;
          next.push({
            claimId: claim.id,
            cite,
            section: claim.section,
            d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
          });
        }
      }
      setEdges(next);
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (containerRef.current) ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, [data]);

  const connected = useMemo(() => {
    const set = new Set<string>();
    if (!hover) return set;
    for (const e of edges) {
      if (e.claimId === hover || `cite-${e.cite}` === hover) {
        set.add(e.claimId);
        set.add(`cite-${e.cite}`);
      }
    }
    return set;
  }, [hover, edges]);

  const active = (id: string) => !hover || hover === id || connected.has(id);
  const usedCites = new Set(data.claims.flatMap((c) => c.cites));

  return (
    <div ref={containerRef} className="relative grow min-h-0 overflow-auto p-3">
      <svg className="absolute inset-0 w-full h-full pointer-events-none" style={{ minHeight: '100%' }}>
        {edges.map((e, i) => {
          const lit = !hover || e.claimId === hover || `cite-${e.cite}` === hover;
          return (
            <path
              key={i}
              d={e.d}
              fill="none"
              stroke={SECTION_EDGE[e.section % SECTION_EDGE.length]}
              strokeWidth={lit && hover ? 2.5 : 1.5}
              opacity={lit ? (hover ? 0.9 : 0.45) : 0.08}
            />
          );
        })}
      </svg>
      <div className="relative flex gap-10">
        {/* Claims, grouped by brief section */}
        <div className="flex-1 flex flex-col gap-3 min-w-0">
          <div className="rounded-lg border border-base-300 bg-base-200 p-2 text-xs italic">
            “{data.question}”
          </div>
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
                    onMouseEnter={() => setHover(claim.id)}
                    onMouseLeave={() => setHover(null)}
                    className={cn(
                      'rounded-lg border-l-4 border border-base-300 bg-base-100 p-2 text-[0.7rem] leading-snug transition-opacity cursor-default',
                      SECTION_BORDER[claim.section % SECTION_BORDER.length],
                      !active(claim.id) && 'opacity-30',
                    )}
                  >
                    {claim.text.length > 220 ? `${claim.text.slice(0, 220)}…` : claim.text}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        {/* Sources */}
        <div className="w-64 shrink-0 flex flex-col gap-1.5">
          <div className="text-[0.65rem] font-semibold uppercase tracking-wide text-base-content/50">
            Sources
          </div>
          {data.citations.map((c) => (
            <div
              key={c.n}
              ref={register(`cite-${c.n}`)}
              onMouseEnter={() => setHover(`cite-${c.n}`)}
              onMouseLeave={() => setHover(null)}
            >
              <SourceNode
                citation={c}
                hovered={hover === `cite-${c.n}` || connected.has(`cite-${c.n}`)}
                dimmed={!active(`cite-${c.n}`) || (!hover && !usedCites.has(c.n))}
              />
              {!usedCites.has(c.n) && (
                <div className="text-[0.6rem] text-base-content/40 px-1">
                  retrieved, not cited by any claim
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

const ProvenancePanel: FC = () => {
  const messages = useChatStore((s) => s.messages);
  const latest = useMemo(() => latestBrief(messages), [messages]);
  const data = useMemo(
    () => (latest ? parseProvenance(latest.brief, latest.citations, latest.question) : null),
    [latest],
  );

  return (
    <div className="basis-1 grow flex flex-col min-h-0 border-l border-base-300">
      <div className="px-3 py-2 border-b border-base-300 flex items-center gap-2">
        <Waypoints className="w-4 h-4" />
        <span className="text-sm font-semibold">Insight provenance</span>
        <span className="text-xs text-base-content/50">
          every claim, traced to its sources — hover to follow a path
        </span>
      </div>
      {data ? (
        <Graph data={data} />
      ) : (
        <div className="grow flex items-center justify-center p-8 text-center text-sm text-base-content/50">
          Ask a food-security question — the path from each claim in the answer back to its
          source documents will render here.
        </div>
      )}
    </div>
  );
};

export default ProvenancePanel;
