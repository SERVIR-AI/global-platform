import type { ChatItem, ChatResponse, Citation, Grounded } from '@/types/chat';

/**
 * Derive the full construction path of a brief from what the API returns:
 * question -> parse (model) -> feeds (retrieval queries / conditions / calendar)
 * -> evidence chunks (grouped by document) -> synthesis gate -> claims.
 * Pure client-side — the graph is a second rendering of the same receipts.
 */

export interface Claim {
  id: string;
  section: number;
  text: string;
  cites: number[];
}

export interface Feed {
  id: string;
  label: string;
  /** The literal query / receipt behind this step. */
  detail?: string;
  cites: number[];
  /** Honest zero: the step ran and matched nothing. */
  empty?: boolean;
}

export interface DocGroup {
  key: string;
  source: string;
  title?: string | null;
  url?: string | null;
  archived_copy?: string | null;
  kind: Citation['kind'];
  adjusted?: boolean;
  citations: Citation[];
}

export interface ProvenanceData {
  question: string;
  parsed?: { crop?: string; country?: string; focus?: string } | null;
  model?: string;
  grounded?: Grounded | null;
  sections: string[];
  claims: Claim[];
  citations: Citation[];
  feeds: Feed[];
  docs: DocGroup[];
}

// Mirrors the backend's _cited_numbers exactly: clusters split, ranges expanded
// (capped), 4-digit values treated as years — the graph must count citations the
// same way the groundedness gate does.
const citesIn = (text: string): number[] => {
  const nums = new Set<number>();
  for (const m of text.matchAll(/\[([\d\s,\-–]+)\]/g)) {
    for (const part of m[1].replace(/–/g, '-').split(/[\s,]+/)) {
      if (!part) continue;
      const range = part.match(/^(\d+)-(\d+)$/);
      if (range) {
        const a = Number(range[1]);
        const b = Math.min(Number(range[2]), a + 50);
        for (let n = a; n <= b; n++) nums.add(n);
      } else if (/^\d+$/.test(part)) {
        nums.add(Number(part));
      }
    }
  }
  return [...nums].filter((n) => n > 0 && n < 1000).sort((a, b) => a - b);
};

const buildFeeds = (
  citations: Citation[],
  evidence: Partial<ChatResponse>['evidence'],
): Feed[] => {
  const byKind = (pred: (c: Citation) => boolean) =>
    citations.filter(pred).map((c) => c.n);
  const queries = evidence?.queries;
  // Every feed the backend RUNS is shown — a step that ran and produced nothing
  // renders as an honest gap, never disappears (the backend declares the same
  // gap in the brief's What's-missing section).
  const feeds: Feed[] = [
    {
      id: 'feed-forecast',
      label: 'Library retrieval — current signal',
      detail: queries?.forecast && `query: “${queries.forecast}”`,
      cites: byKind((c) => c.kind === 'document' && c.temporal === 'forecast'),
    },
    {
      id: 'feed-retro',
      label: 'Library retrieval — analog years',
      detail: queries?.retrospective && `query: “${queries.retrospective}”`,
      cites: byKind((c) => c.kind === 'document' && c.temporal !== 'forecast'),
    },
    {
      id: 'feed-conditions',
      label: 'Live conditions query (GEOGLAM)',
      detail:
        citations.find((c) => c.kind === 'conditions')?.query ??
        (evidence?.conditions === false ? 'ran — unavailable or no assessment rows' : undefined),
      cites: byKind((c) => c.kind === 'conditions'),
    },
    {
      id: 'feed-calendar',
      label: 'Crop calendar',
      detail: citations.find((c) => c.kind === 'calendar')
        ? citations.find((c) => c.kind === 'calendar')?.adjusted
          ? 'requester-adjusted for this run'
          : 'hub default'
        : 'no calendar configured for this target',
      cites: byKind((c) => c.kind === 'calendar'),
    },
  ];
  return feeds.map((f) => ({ ...f, empty: f.cites.length === 0 }));
};

const buildDocs = (citations: Citation[]): DocGroup[] => {
  const groups = new Map<string, DocGroup>();
  for (const c of citations) {
    const key = c.kind === 'document' ? (c.doc_id ?? `doc-${c.n}`) : `${c.kind}-${c.n}`;
    const g = groups.get(key) ?? {
      key,
      source: c.source ?? 'unknown source',
      title: c.title,
      url: c.url,
      archived_copy: c.archived_copy,
      kind: c.kind,
      adjusted: c.adjusted,
      citations: [],
    };
    g.citations.push(c);
    groups.set(key, g);
  }
  return [...groups.values()];
};

export const parseProvenance = (
  item: Partial<ChatResponse>,
  question: string,
): ProvenanceData => {
  const brief = item.brief ?? '';
  const citations = item.citations ?? [];
  const body = brief.split(/\n## Sources\b/)[0];
  const sections: string[] = [];
  const claims: Claim[] = [];
  let current = -1;
  const addClaim = (text: string) => {
    claims.push({
      id: `claim-${claims.length}`,
      section: current,
      text: text.replace(/\[([\d\s,\-–]+)\]/g, '').replace(/\s+/g, ' ').trim(),
      cites: citesIn(text),
    });
  };
  for (const block of body.split('\n\n')) {
    const text = block.trim();
    if (!text) continue;
    if (text.startsWith('#')) {
      // A header may share its block with prose (single newline) — the backend
      // gate accepts that form, so the graph must not swallow the claim.
      const lines = text.split('\n');
      sections.push(lines[0].replace(/^#+\s*/, ''));
      current = sections.length - 1;
      const rest = lines.slice(1).filter((l) => !l.trim().startsWith('#')).join('\n').trim();
      if (rest) addClaim(rest);
      continue;
    }
    if (current < 0) {
      // Pre-section prose still counts as a claim; give it a home.
      sections.push('Preamble');
      current = sections.length - 1;
    }
    addClaim(text);
  }
  return {
    question,
    parsed: item.parsed ?? null,
    model: item.model,
    grounded: item.grounded ?? null,
    sections,
    claims,
    citations,
    feeds: buildFeeds(citations, item.evidence),
    docs: buildDocs(citations),
  };
};

/** The latest answered brief in the conversation, with the question that asked it. */
export const latestBrief = (
  messages: ChatItem[],
): { item: Partial<ChatResponse>; question: string } | null => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const item = messages[i] as ChatItem & Partial<ChatResponse>;
    if (item.brief && item.declined !== true) {
      let question = '';
      for (let j = i - 1; j >= 0; j--) {
        const prev = messages[j];
        if ('messages' in prev) {
          question = prev.messages[prev.messages.length - 1]?.content ?? '';
          break;
        }
      }
      return { item, question };
    }
  }
  return null;
};
