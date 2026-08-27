/**
 * Embed mode — a single platform component rendered standalone for a host page.
 *
 * FAIL-CLOSED by construction: the component is fed ONLY what the platform's
 * resolver returns for the given id. No props carry a verdict, and if the
 * resolver can't be reached the embed says so rather than showing anything.
 * This is the difference between a live verdict and one frozen into markup.
 */
import { useEffect, useState, type FC } from 'react';
import { Graph } from '@/components/Provenance';
import EmbedMap, { type VizPayload } from '@/components/EmbedMap';
import { parseProvenance, type ProvenanceData } from '@/lib/provenance';
import { resolveApiUrl } from '@/lib/api';

const get = async (path: string) => {
  const res = await fetch(resolveApiUrl(path), { headers: { accept: 'application/json' } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
};

/** Rebuild the graph's input from what the platform recorded, by receipt id. */
const loadByReceipt = async (receiptId: string): Promise<ProvenanceData> => {
  const receipt = await get(`/api/resolve/receipt/${receiptId}`);
  const [report, pack] = await Promise.all([
    get(`/api/resolve/report/${receipt.report_id}`),
    get(`/api/resolve/pack/${receipt.pack_id}`),
  ]);
  return parseProvenance(
    {
      brief: report.draft ?? '',
      citations: pack.citations ?? [],
      parsed: { country: pack.country, crop: pack.crop, focus: pack.focus },
      evidence: { ...(pack.stats ?? {}), queries: pack.queries },
      grounded: report.checks ?? null,
    },
    receipt.question ?? '',
  );
};

const Notice: FC<{ title: string; detail?: string }> = ({ title, detail }) => (
  <div className="p-4 text-sm">
    <div className="font-semibold">{title}</div>
    {detail && <div className="opacity-70 mt-1">{detail}</div>}
  </div>
);

/** The viz payload a risk receipt's pack carries, by receipt id. */
const loadVizByReceipt = async (receiptId: string): Promise<VizPayload> => {
  const receipt = await get(`/api/resolve/receipt/${receiptId}`);
  const pack = await get(`/api/resolve/pack/${receipt.pack_id}`);
  if (!pack.viz) throw new Error('this receipt carries no map payload');
  return pack.viz as VizPayload;
};

const EMBEDDABLE = ['provenance_graph', 'hazard_map'] as const;

const Embed: FC = () => {
  const params = new URLSearchParams(window.location.search);
  const component = params.get('embed');
  const receiptId = params.get('receipt_id');
  const [data, setData] = useState<ProvenanceData | null>(null);
  const [viz, setViz] = useState<VizPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!receiptId) return;
    if (component === 'provenance_graph')
      loadByReceipt(receiptId).then(setData).catch((e) => setError(String(e)));
    else if (component === 'hazard_map')
      loadVizByReceipt(receiptId).then(setViz).catch((e) => setError(String(e)));
  }, [component, receiptId]);

  if (!EMBEDDABLE.includes(component as (typeof EMBEDDABLE)[number]))
    return <Notice title="Unknown embed" detail={`No embeddable component named "${component}".`} />;
  if (!receiptId)
    return <Notice title="unverified by this platform" detail="No receipt_id supplied — nothing to resolve." />;
  if (error)
    return <Notice title="unverified by this platform" detail={`Could not resolve receipt ${receiptId}: ${error}`} />;
  if (component === 'hazard_map') {
    if (!viz) return <Notice title="Resolving…" detail={`receipt ${receiptId}`} />;
    return (
      <div className="h-screen">
        <EmbedMap viz={viz} />
      </div>
    );
  }
  if (!data) return <Notice title="Resolving…" detail={`receipt ${receiptId}`} />;
  return <Graph data={data} />;
};

export default Embed;
