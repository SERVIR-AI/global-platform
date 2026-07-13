import type { ChatItem, ChatResponse, Citation } from '@/types/chat';

/**
 * Derive the insight->source structure of a brief entirely from what the API
 * already returns: sections from `##` headers, one claim per paragraph, and the
 * [n] markers each claim cites. No backend involvement — the graph is a second
 * rendering of the same receipts.
 */

export interface Claim {
  id: string;
  section: number;
  text: string;
  cites: number[];
}

export interface ProvenanceData {
  question: string;
  sections: string[];
  claims: Claim[];
  citations: Citation[];
}

const citesIn = (text: string): number[] => {
  const nums = new Set<number>();
  for (const m of text.matchAll(/\[([\d\s,\-–]+)\]/g)) {
    for (const part of m[1].split(/[\s,]+/)) {
      const n = Number(part.replace(/[–-].*$/, ''));
      if (Number.isInteger(n) && n > 0 && n < 1000) nums.add(n);
    }
  }
  return [...nums].sort((a, b) => a - b);
};

export const parseProvenance = (
  brief: string,
  citations: Citation[],
  question: string,
): ProvenanceData => {
  const body = brief.split(/\n## Sources\b/)[0];
  const sections: string[] = [];
  const claims: Claim[] = [];
  let current = -1;
  for (const block of body.split('\n\n')) {
    const text = block.trim();
    if (!text) continue;
    if (text.startsWith('##')) {
      sections.push(text.replace(/^#+\s*/, ''));
      current = sections.length - 1;
      continue;
    }
    if (current < 0) continue;
    claims.push({
      id: `claim-${claims.length}`,
      section: current,
      text: text.replace(/\[([\d\s,\-–]+)\]/g, '').replace(/\s+/g, ' ').trim(),
      cites: citesIn(text),
    });
  }
  return { question, sections, claims, citations };
};

/** The latest answered brief in the conversation, with the question that asked it. */
export const latestBrief = (
  messages: ChatItem[],
): { brief: string; citations: Citation[]; question: string } | null => {
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
      return { brief: item.brief, citations: item.citations ?? [], question };
    }
  }
  return null;
};
