/**
 * Area monitor — the living view of ONE named area, for every pack that takes a place.
 *
 * It computes nothing. It lists the receipts already minted for the area (newest
 * first) and renders the latest one's recorded map, class series, transitions,
 * factor arithmetic and sources, all resolved from /api/resolve/* at view time.
 * With no place it shows the living list of areas to pick from. Fail-closed like
 * every embed: an unreachable resolver reads "unverified", never a blank panel.
 */
import { useEffect, useState, type FC } from 'react';
import EmbedMap, { type VizPayload } from '@/components/EmbedMap';
import { resolveApiUrl } from '@/lib/api';

const get = async (path: string) => {
  const res = await fetch(resolveApiUrl(path), { headers: { accept: 'application/json' } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
};

type ReceiptRow = {
  receipt_id: string; question?: string; pack?: string; passed?: boolean | null;
  target?: Record<string, unknown>; minted_at?: string; pack_id?: string;
};
type Citation = { n?: number; kind?: string; title?: string; text?: string; series?: { points?: { t: string; v: number }[]; unit?: string } };
type Pack = {
  pack?: string; citations?: Citation[]; viz?: VizPayload; gaps?: string[];
  stats?: { aoi?: { name?: string; area_km2?: number; how?: string }; classes_km2?: Record<string, number>;
    factors?: Record<string, Record<string, { class: string; pool?: string; factor: number; unit?: string; value: number; value_low?: number; value_high?: number; basis?: string }[]>> };
};
type Receipt = { receipt_id: string; question?: string; pack?: string; pack_id: string; minted_at?: string; passed?: boolean;
  sources?: { title?: string; source?: string; vintage?: string; validation?: string; kind?: string }[] };
type PlacesList = { status: string; layers?: { layer: string; title?: string; areas: number; names: { name: string; km2: number; size: string; qualifier?: string }[]; note?: string }[]; sizes?: Record<string, string> };

const Notice: FC<{ title: string; detail?: string }> = ({ title, detail }) => (
  <div className="p-4 text-sm"><div className="font-semibold">{title}</div>{detail && <div className="opacity-70 mt-1">{detail}</div>}</div>
);

/** One line per source organisation and vintage — a receipt lists a source once per citation. */
const dedupe = (rows: NonNullable<Receipt['sources']>) => {
  const seen = new Set<string>(); const out: typeof rows = [];
  for (const r of rows) { const k = `${r.source ?? r.title}|${r.vintage ?? ''}`; if (!seen.has(k)) { seen.add(k); out.push(r); } }
  return out;
};

const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toLocaleString(undefined, { maximumFractionDigits: 1 }));

/** A small horizontal bar chart for a class series (classes or years → km²) — SVG, no dependency. */
const Series: FC<{ title: string; points: { t: string; v: number }[]; unit?: string }> = ({ title, points, unit }) => {
  const max = Math.max(...points.map((p) => p.v), 0) || 1;
  const w = 560, rowH = 22, labelW = 170, h = points.length * rowH + 8;
  return (
    <figure className="my-3">
      <figcaption className="text-sm font-semibold mb-1">{title}{unit ? <span className="opacity-60 font-normal"> · {unit}</span> : null}</figcaption>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full max-w-xl" role="img" aria-label={title}>
        {points.map((p, i) => {
          const y = i * rowH + 4;
          const bw = Math.max(2, Math.round(((w - labelW - 70) * p.v) / max));
          return (
            <g key={p.t}>
              <text x={labelW - 8} y={y + 15} fontSize={12} textAnchor="end" fill="currentColor">{p.t}</text>
              <rect x={labelW} y={y + 3} width={bw} height={rowH - 8} rx={3} fill="var(--grp-primary, #2380b0)" opacity={0.85} />
              <text x={labelW + bw + 6} y={y + 15} fontSize={12} fill="currentColor" opacity={0.8}>{fmt(p.v)}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
};

/** The living list: every contributed boundary, by layer, with km² and size. */
const Picker: FC<{ onPick: (name: string) => void }> = ({ onPick }) => {
  const [list, setList] = useState<PlacesList | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { get('/api/resolve/places').then(setList).catch((e) => setErr(String(e))); }, []);
  if (err) return <Notice title="unverified by this platform" detail={`could not list areas: ${err}`} />;
  if (!list) return <Notice title="Resolving…" detail="the areas you can name" />;
  if (!list.layers?.length) return <Notice title="No areas yet" detail="no contributed boundary layer carries names — contribute one and it appears here" />;
  return (
    <div className="p-4 text-sm">
      <h2 className="text-lg font-semibold">Areas you can name</h2>
      {list.sizes && <div className="opacity-70 mt-1 mb-3">{Object.entries(list.sizes).map(([k, v]) => `${k}: ${v}`).join(' · ')}</div>}
      {list.layers.map((l) => (
        <section key={l.layer} className="mb-4">
          <h3 className="font-semibold">{l.title || l.layer} <span className="opacity-60 font-normal">({l.areas} areas · {l.layer})</span></h3>
          {l.note && <div className="opacity-70 text-xs">{l.note}</div>}
          <ul className="mt-1 columns-2 md:columns-3 gap-4">
            {l.names.map((n) => (
              <li key={`${l.layer}:${n.name}:${n.qualifier ?? ''}`} className="break-inside-avoid">
                <button type="button" className="link link-hover text-left" onClick={() => onPick(n.name)}>{n.name}</button>
                <span className="opacity-60"> {fmt(n.km2)} km² · {n.size}{n.qualifier ? ` · ${n.qualifier}` : ''}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
};

const AreaMonitor: FC<{ place: string | null; pack?: string | null }> = ({ place, pack }) => {
  const [rows, setRows] = useState<ReceiptRow[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [latest, setLatest] = useState<{ receipt: Receipt; pack: Pack } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!place) return;
    const q = new URLSearchParams({ place, ...(pack ? { pack } : {}), limit: '20' });
    get(`/api/resolve/receipts?${q}`)
      .then(async (r) => {
        setRows(r.receipts ?? []);
        setNote(r.note ?? null);
        const first = (r.receipts ?? [])[0] as ReceiptRow | undefined;
        if (!first) return;
        const receipt = (await get(`/api/resolve/receipt/${first.receipt_id}`)) as Receipt;
        const p = (await get(`/api/resolve/pack/${receipt.pack_id}`)) as Pack;
        setLatest({ receipt, pack: p });
      })
      .catch((e) => setErr(String(e)));
  }, [place, pack]);

  if (!place) return <Picker onPick={(name) => { const u = new URL(window.location.href); u.searchParams.set('place', name); window.location.assign(u.toString()); }} />;
  if (err) return <Notice title="unverified by this platform" detail={`could not resolve ${place}: ${err}`} />;
  if (!rows) return <Notice title="Resolving…" detail={place} />;
  if (!rows.length) return <Notice title={place} detail={note ?? 'no receipt names this area yet'} />;

  const p = latest?.pack; const c = p?.citations ?? [];
  const series = c.filter((x) => x.kind === 'class_series' && x.series?.points?.length);
  const areas = c.filter((x) => x.kind === 'class_area' && x.series?.points?.length);
  const transitions = c.filter((x) => x.kind === 'class_transitions');
  const factors = p?.stats?.factors ?? {};
  const viz = p?.viz && p.viz.aoi && p.viz.bounds && (p.viz.hazard_layer?.raster_url || p.viz.hazard_layer?.geojson) ? p.viz : null;
  const aoi = p?.stats?.aoi;

  return (
    <div className="p-4 text-sm space-y-4">
      <header>
        <h2 className="text-lg font-semibold">{aoi?.name || place}</h2>
        <div className="opacity-70">{aoi ? `${fmt(aoi.area_km2 ?? 0)} km² · ${aoi.how ?? ''}` : ''}</div>
        {latest && (
          <div className="mt-1">latest: <em>{latest.receipt.question}</em> · {latest.receipt.pack} · receipt <a className="link" href={resolveApiUrl(`/api/resolve/receipt/${latest.receipt.receipt_id}`)} target="_blank" rel="noreferrer">{latest.receipt.receipt_id}</a>{latest.receipt.passed === false && ' · DID NOT PASS'}</div>
        )}
      </header>
      {viz && <div className="h-96 rounded-box overflow-hidden border border-base-300"><EmbedMap viz={viz} /></div>}
      {!viz && latest && <Notice title="no map recorded with this receipt" />}
      {series.map((s) => <Series key={s.n} title={s.title ?? 'series'} points={s.series!.points!} unit={s.series?.unit} />)}
      {!series.length && areas.slice(0, 1).map((s) => <Series key={s.n} title={s.title ?? 'by class'} points={s.series!.points!.filter((pt) => pt.v > 0)} unit={s.series?.unit} />)}
      {transitions.length > 0 && (
        <section><h3 className="font-semibold">Change</h3>{transitions.map((t) => <p key={t.n} className="mt-1">{t.text}</p>)}</section>
      )}
      {Object.keys(factors).length > 0 && (
        <section>
          <h3 className="font-semibold">Factor × area (defaults applied, not measurements)</h3>
          <table className="table table-xs mt-1"><thead><tr><th>table</th><th>class</th><th>pool</th><th>factor</th><th>result</th></tr></thead>
            <tbody>{Object.entries(factors).flatMap(([ds, byClass]) => Object.values(byClass).flat().map((r, i) => (
              <tr key={`${ds}:${i}`}><td>{ds}</td><td>{r.class}</td><td>{r.pool ?? ''}</td><td>{fmt(r.factor)} {r.unit ?? ''}</td><td>{fmt(r.value)}{r.value_low != null && r.value_high != null ? ` (${fmt(r.value_low)}–${fmt(r.value_high)})` : ''}</td></tr>
            )))}</tbody></table>
        </section>
      )}
      {latest?.receipt.sources?.length ? (
        <section><h3 className="font-semibold">Sources behind the latest answer</h3>
          <ul className="mt-1 list-disc pl-5">{dedupe(latest.receipt.sources).map((s, i) => <li key={i}>{s.source ?? s.title}{s.vintage ? ` · ${s.vintage}` : ''}{s.validation ? ` · ${s.validation}` : ''}</li>)}</ul>
        </section>
      ) : null}
      {p?.gaps?.length ? <section><h3 className="font-semibold">Declared gaps</h3><ul className="mt-1 list-disc pl-5">{p.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul></section> : null}
      <section>
        <h3 className="font-semibold">Everything asked about this area</h3>
        <ul className="mt-1 list-disc pl-5">{rows.map((r) => (
          <li key={r.receipt_id}><a className="link" href={`?embed=hazard_map&receipt_id=${r.receipt_id}`} target="_blank" rel="noreferrer">{r.question}</a> <span className="opacity-60">· {r.pack} · {r.minted_at?.slice(0, 16).replace('T', ' ')}{r.passed === false ? ' · did not pass' : ''}</span></li>
        ))}</ul>
      </section>
    </div>
  );
};

export default AreaMonitor;
