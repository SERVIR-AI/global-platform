import { resolveApiUrl } from '@/lib/api';
import { cn } from '@/lib/utils';
import type { Citation, Grounded } from '@/types/chat';
import { Archive, Check, Copy, ExternalLink, TriangleAlert } from 'lucide-react';
import { FC, useState } from 'react';

/**
 * The trust surface of a food-security brief, built for the doubts raised on
 * the food-systems calls: every insight traces to a source in two clicks
 * (chip -> card -> original/archived document), validation levels are worn as
 * badges, the groundedness checks render as a QA stamp that also confesses
 * what it could NOT verify, and declines are first-class content.
 */

const VALIDATION_STYLE: Record<string, string> = {
  'multi-agency-consensus': 'badge-success',
  'peer-reviewed': 'badge-info',
  'single-agency': 'badge-ghost',
  'model-output': 'badge-warning',
};

const VALIDATION_LABEL: Record<string, string> = {
  'multi-agency-consensus': 'Multi-agency consensus',
  'peer-reviewed': 'Peer-reviewed',
  'single-agency': 'Single agency',
  'model-output': 'Model output',
};

/** Scroll to and briefly highlight a source card when its [n] chip is clicked. */
export const jumpToCitation = (bubbleId: string, n: number) => {
  const el = document.getElementById(`${bubbleId}-cite-${n}`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('ring-2', 'ring-primary');
  setTimeout(() => el.classList.remove('ring-2', 'ring-primary'), 1600);
};

const CheckChip: FC<{ ok: boolean; label: string }> = ({ ok, label }) => (
  <span
    className={cn(
      'inline-flex items-center gap-1 rounded px-1.5 py-0.5',
      ok ? 'bg-success/15 text-success-content/80' : 'bg-warning/20 text-warning-content',
    )}
  >
    {ok ? <Check className="w-3 h-3" /> : <TriangleAlert className="w-3 h-3" />}
    {label}
  </span>
);

/** The QA stamp: what was checked, what passed, and what the system admits. */
export const GroundedStrip: FC<{
  grounded: Grounded;
  citations: Citation[];
  model?: string;
}> = ({ grounded, citations, model }) => {
  const unverified = grounded.numbers_unverified ?? [];
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[0.7rem] text-base-content/70">
      <CheckChip
        ok={grounded.phantom_citations.length === 0}
        label={`${grounded.cited.length}/${citations.length} sources cited, all resolve`}
      />
      <CheckChip ok={grounded.uncited_paragraphs.length === 0} label="every paragraph sourced" />
      <CheckChip ok={grounded.missing_sections.length === 0} label="structure complete" />
      <CheckChip
        ok={unverified.length === 0}
        label={
          unverified.length === 0
            ? 'all figures traced to evidence'
            : `figures not found in evidence: ${unverified.slice(0, 4).join(', ')}`
        }
      />
      {(grounded.attempts ?? 1) > 1 && (
        <CheckChip ok={false} label={`passed on attempt ${grounded.attempts}`} />
      )}
      {model && <span className="opacity-60">written by {model}</span>}
    </div>
  );
};

const SourceCard: FC<{ citation: Citation; bubbleId: string }> = ({ citation: c, bubbleId }) => (
  <div
    id={`${bubbleId}-cite-${c.n}`}
    className="rounded-lg border border-base-300 bg-base-100 p-2 text-xs transition-shadow"
  >
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="font-mono font-bold text-primary">[{c.n}]</span>
      <span className="font-semibold">{c.source ?? 'unknown source'}</span>
      {c.title && <span className="text-base-content/70">— {c.title}</span>}
      {c.pub_date && <span className="text-base-content/50">({c.pub_date})</span>}
    </div>
    <div className="mt-1 flex flex-wrap items-center gap-1.5">
      {c.validation && (
        <span className={cn('badge badge-xs', VALIDATION_STYLE[c.validation] ?? 'badge-ghost')}>
          {VALIDATION_LABEL[c.validation] ?? c.validation}
        </span>
      )}
      {c.temporal && (
        <span className="badge badge-xs badge-outline">
          {c.temporal === 'forecast' ? 'projection' : 'record'}
        </span>
      )}
      {c.kind === 'conditions' && <span className="badge badge-xs badge-outline">live data feed</span>}
      {typeof c.score === 'number' && (
        <span className="text-base-content/40">relevance {c.score.toFixed(2)}</span>
      )}
      <span className="grow" />
      {c.url && (
        <a href={c.url} target="_blank" rel="noreferrer" className="link inline-flex items-center gap-0.5">
          <ExternalLink className="w-3 h-3" />
          original
        </a>
      )}
      {c.archived_copy && (
        <a
          href={resolveApiUrl(c.archived_copy)}
          target="_blank"
          rel="noreferrer"
          className="link inline-flex items-center gap-0.5"
          title="The exact document this system read — served locally, immune to the source link changing"
        >
          <Archive className="w-3 h-3" />
          archived copy
        </a>
      )}
    </div>
    {c.query && (
      <div className="mt-1 font-mono text-[0.65rem] text-base-content/50 break-all">
        query: {c.query}
      </div>
    )}
  </div>
);

/** The Sources panel: one card per evidence entry, jump targets for the chips. */
export const BriefSources: FC<{ citations: Citation[]; bubbleId: string }> = ({
  citations,
  bubbleId,
}) => (
  <div className="flex flex-col gap-1.5">
    <div className="text-xs font-semibold text-base-content/70">
      Sources — every claim above traces to one of these
    </div>
    {citations.map((c) => (
      <SourceCard key={c.n} citation={c} bubbleId={bubbleId} />
    ))}
  </div>
);

/** A refusal is an answer: calm, explicit about what's missing, never an error. */
export const DeclineCard: FC<{ reason: string }> = ({ reason }) => (
  <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
    <div className="font-semibold flex items-center gap-1.5">
      <TriangleAlert className="w-4 h-4" />
      The system declined to answer
    </div>
    <p className="mt-1">{reason.replace(/^DECLINE:\s*/, '')}</p>
    <p className="mt-1 text-xs text-base-content/60">
      Declining beats guessing: this answer would not have met the evidence bar.
    </p>
  </div>
);

/** Copies the FULL brief markdown including its Sources block — the sanctioned
 *  copy path, so what leaves this screen carries its receipts. */
export const CopyBriefButton: FC<{ brief: string }> = ({ brief }) => {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-xs btn-ghost gap-1 text-base-content/60"
      onClick={() => {
        void navigator.clipboard.writeText(brief).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
      {copied ? 'copied with sources' : 'copy brief (with sources)'}
    </button>
  );
};
