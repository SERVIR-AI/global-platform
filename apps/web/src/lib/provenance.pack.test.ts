import { describe, expect, it } from 'vitest';
import { parseProvenance } from './provenance';

describe('provenance lanes follow the pack', () => {
  const citations = [
    { n: 1, kind: 'class_area', source: 'GMW' },
    { n: 2, kind: 'class_factor', source: 'IPCC' },
    { n: 3, kind: 'document', source: 'CATIE', temporal: 'retrospective' },
    { n: 4, kind: 'vector_summary', source: 'SINAC' },
    { n: 5, kind: 'gaps', source: 'platform' },
  ] as never[];

  it('a land-cover receipt shows the steps it ran, not food-security furniture', () => {
    const d = parseProvenance(
      { brief: '## A\nx [1][2]', citations, evidence: { queries: ["retrieve 'mangrove' -> 2 hits"] } as never },
      'q', 'land-cover');
    const labels = d.feeds.map((f) => f.label);
    expect(labels).toEqual([
      'Class areas counted from the map', 'Factor table applied (area × factor)',
      'Library retrieval', 'Contributed layers measured inside the area']);
    expect(labels.join(' ')).not.toMatch(/GEOGLAM|Crop calendar/);
    expect(d.feeds.find((f) => f.label === 'Library retrieval')?.detail).toContain('mangrove');
  });

  it('a food-security receipt keeps its four steps', () => {
    const d = parseProvenance({ brief: '## A\nx [1]', citations: [] }, 'q', 'food-security');
    expect(d.feeds.map((f) => f.label)).toContain('Live conditions query (GEOGLAM)');
  });

  it('an unknown kind is named after itself', () => {
    const d = parseProvenance({ brief: '', citations: [{ n: 1, kind: 'new_thing', source: 's' }] as never[] }, 'q', 'x-pack');
    expect(d.feeds[0].label).toBe('new thing');
  });
});
