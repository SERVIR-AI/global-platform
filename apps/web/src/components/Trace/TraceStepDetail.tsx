import type { FieldAudience } from '@/lib/trace/fields';
import { toStepFields } from '@/lib/trace/fields';
import { WHY_HEADING } from '@/lib/trace/labels';
import { formatDuration } from '@/lib/trace/selectors';
import type { Legend } from '@/types/chat';
import type { TraceStep } from '@/types/trace';
import { FC } from 'react';
import FieldValue from './FieldValue';

type TraceStepDetailProps = {
  step: TraceStep;
  title: string;
  detail: FieldAudience;
  /** The turn's severity scale, used to name the by-class breakdown. */
  legend: Legend | null;
};

/** Section heading, shared by the `why` block and every field group. */
const SectionHeading: FC<{ children: string }> = ({ children }) => (
  <div className="font-medium text-base-content/60 uppercase tracking-wide text-[0.65rem]">
    {children}
  </div>
);

/**
 * Tier 3 — everything worth knowing about one step.
 *
 * Holds no field knowledge of its own: `toStepFields` decides which fields exist, which
 * audience each belongs to, and what a missing one means. Swapping this component for a
 * different presentation (a table, a printable block) needs no change to `fields.ts`.
 */
const TraceStepDetail: FC<TraceStepDetailProps> = ({ step, title, detail, legend }) => {
  const groups = toStepFields(step, detail, legend);

  return (
    <div className="rounded-lg bg-base-200/60 p-3 flex flex-col gap-3">
      <div>
        <div className="font-medium">{title}</div>
        <p className="text-base-content/50">Took {formatDuration(step.duration_ms)}</p>
      </div>

      <div className="flex flex-col gap-1">
        <SectionHeading>{WHY_HEADING}</SectionHeading>
        {/* Backend-authored copy. `tracing.py` writes `why` per node; render it verbatim. */}
        <p className="text-base-content/70">{step.why}</p>
      </div>

      {groups.map((group) => (
        <div key={group.group} className="flex flex-col gap-1">
          <SectionHeading>{group.group}</SectionHeading>
          <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-3 gap-y-1">
            {group.fields.map((field) => (
              <div key={field.key} className="contents">
                <dt
                  className={
                    field.hint ? 'text-base-content/60 cursor-help' : 'text-base-content/60'
                  }
                  title={field.hint}
                >
                  {field.label}
                </dt>
                <dd className="min-w-0">
                  <FieldValue value={field.value} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
};

export default TraceStepDetail;
