import { ApiError, uploadTiff } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import type { TiffUploadResponse } from '@/types/chat';
import { useMutation } from '@tanstack/react-query';
import { Check, Database, X } from 'lucide-react';
import { FC, useRef, useState } from 'react';

/** A compact "what we detected" line from the verification report's observed stats. */
const observedSummary = (o: TiffUploadResponse['observed']): string =>
  [
    o.dtype,
    o.crs_epsg ? `EPSG:${o.crs_epsg}` : null,
    o.width && o.height ? `${o.width}×${o.height}` : null,
    o.sampled_min != null && o.sampled_max != null
      ? `values ${o.sampled_min}–${o.sampled_max}`
      : null,
  ]
    .filter(Boolean)
    .join(' · ');

/**
 * "Bring your own data" — upload a GeoTIFF hazard layer. The file is verified
 * server-side (`POST /api/tiffs`) and registered for the current chat thread only
 * on a PASS; the verification report (or rejection reasons) is shown inline.
 */
const DataUpload: FC = () => {
  const threadId = useChatStore((s) => s.threadId);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [hazard, setHazard] = useState('flood');
  const [scale, setScale] = useState<'0-5' | '1-5'>('0-5');

  const mutation = useMutation<TiffUploadResponse, unknown, FormData>({
    mutationFn: (form) => uploadTiff(form),
  });

  const submit = () => {
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    form.append('thread_id', threadId);
    form.append('hazard_label', hazard.trim() || 'hazard');
    form.append('severity_scale', scale);
    mutation.mutate(form);
  };

  const report = mutation.data;
  const errMsg =
    mutation.error instanceof ApiError
      ? ((mutation.error.body as { detail?: string })?.detail ??
        `Upload failed (${mutation.error.status}).`)
      : mutation.error
        ? 'Upload failed.'
        : null;

  return (
    <>
      <button
        type="button"
        className="link text-xs text-zinc-400"
        onClick={() => dialogRef.current?.showModal()}
      >
        <Database className="inline mr-1 w-3 h-3" />
        Bring your own data
      </button>

      <dialog ref={dialogRef} className="modal" aria-labelledby="byod-title">
        <div className="modal-box">
          <h3 id="byod-title" className="mb-1 text-lg font-semibold">
            Bring your own data
          </h3>
          <p className="mb-4 text-sm text-zinc-500">
            Upload a GeoTIFF hazard layer — a single-band severity raster on a 0–5 or 1–5 scale.
            It&apos;s verified before it can be used; once it passes, just ask about it in chat.
          </p>

          <div className="flex flex-col gap-3">
            <input
              type="file"
              accept=".tif,.tiff"
              aria-label="GeoTIFF file"
              className="file-input file-input-sm w-full"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <label className="flex items-center gap-2 text-sm">
              <span className="w-28 shrink-0 text-zinc-500">Hazard</span>
              <input
                type="text"
                value={hazard}
                placeholder="e.g. flood"
                onChange={(e) => setHazard(e.target.value)}
                className="input input-sm w-full"
              />
            </label>
            <label className="flex items-center gap-2 text-sm">
              <span className="w-28 shrink-0 text-zinc-500">Severity scale</span>
              <select
                value={scale}
                onChange={(e) => setScale(e.target.value as '0-5' | '1-5')}
                className="select select-sm w-full"
              >
                <option value="0-5">0–5</option>
                <option value="1-5">1–5</option>
              </select>
            </label>
          </div>

          <div role="status" aria-live="polite">
            {errMsg && <p className="mt-3 text-sm text-error">{errMsg}</p>}

            {report && (
              <div
                className={cn(
                  'mt-3 rounded-lg p-3 text-sm',
                  report.ok ? 'bg-success/10' : 'bg-error/10',
                )}
              >
                {report.ok ? (
                  <>
                    <p className="text-success">
                      <Check className="inline mr-1 w-4 h-4" />
                      Verified and added. Ask about it in chat — e.g.{' '}
                      <span className="italic">
                        &ldquo;roads in my uploaded {report.hazard_label} layer in Battambang&rdquo;
                      </span>
                      .
                    </p>
                    {observedSummary(report.observed) && (
                      <p className="mt-1 text-xs text-zinc-500">
                        Detected: {observedSummary(report.observed)}
                      </p>
                    )}
                  </>
                ) : (
                  <>
                    <p className="font-medium text-error">
                      <X className="inline mr-1 w-4 h-4" />
                      Rejected — failed verification:
                    </p>
                    <ul className="mt-1 list-inside list-disc text-error">
                      {report.mismatches.map((m, i) => (
                        <li key={i}>{m}</li>
                      ))}
                    </ul>
                  </>
                )}
                {report.warnings.length > 0 && (
                  <ul className="mt-2 list-inside list-disc text-warning">
                    {report.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>

          <div className="modal-action">
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={!file || mutation.isPending}
              onClick={submit}
            >
              {mutation.isPending ? 'Verifying…' : 'Upload & verify'}
            </button>
            <form method="dialog">
              <button className="btn btn-sm">Close</button>
            </form>
          </div>
        </div>
        <form method="dialog" className="modal-backdrop">
          <button>close</button>
        </form>
      </dialog>
    </>
  );
};

export default DataUpload;
