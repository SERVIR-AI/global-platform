import type { TraceFieldValue } from '@/lib/trace/fields';
import { Check, X } from 'lucide-react';
import { FC } from 'react';

type FieldValueProps = {
  value: TraceFieldValue;
};

/**
 * Renders one field value. Deliberately the only place that knows how a
 * `TraceFieldValue` variant looks on screen — `fields.ts` decides WHAT to show, this
 * decides how, and the two are swapped independently.
 *
 * The `missing` branch is the important one: an em dash carrying the reason as a tooltip,
 * never a `0` and never an empty cell. Zero is reserved for a genuinely measured zero.
 */
const FieldValue: FC<FieldValueProps> = ({ value }) => {
  switch (value.kind) {
    case 'text':
      return <span className="break-words">{value.text}</span>;

    case 'code':
      return (
        <code className="rounded bg-black/10 px-1 py-0.5 text-[0.9em] break-all">{value.text}</code>
      );

    case 'list':
      return (
        <ul className="flex flex-col gap-0.5">
          {value.items.map((item, index) => (
            <li key={index} className="break-words">
              {item}
            </li>
          ))}
        </ul>
      );

    case 'flag':
      return (
        <span className={value.value ? 'text-success' : 'text-base-content/70'}>
          {value.value ? (
            <Check className="inline w-3.5 h-3.5 mr-1 align-[-2px]" />
          ) : (
            <X className="inline w-3.5 h-3.5 mr-1 align-[-2px]" />
          )}
          {value.label}
        </span>
      );

    case 'missing':
      return (
        <span className="text-base-content/40 cursor-help" title={value.reason}>
          —
        </span>
      );

    case 'json':
      return (
        <details>
          <summary className="cursor-pointer select-none text-base-content/60">show</summary>
          <pre className="mt-1 max-h-64 overflow-auto rounded bg-zinc-900 p-2 font-mono text-[0.7rem] leading-relaxed text-zinc-100">
            {JSON.stringify(value.value, null, 2)}
          </pre>
        </details>
      );

    default: {
      // Unreachable for values built by `fields.ts`; present so a new variant added there
      // fails the build here rather than rendering nothing at runtime.
      const unknownValue: never = value;
      return <span className="text-base-content/40">{String(unknownValue)}</span>;
    }
  }
};

export default FieldValue;
