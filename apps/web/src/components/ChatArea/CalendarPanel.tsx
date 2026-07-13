import { getCropCalendar } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import type { SeasonSpec } from '@/types/chat';
import { useQuery } from '@tanstack/react-query';
import { CalendarCog, RotateCcw } from 'lucide-react';
import { FC, useState } from 'react';

/**
 * The ministry ask from the food-systems calls, verbatim: "the standardized
 * calendar assumes the season starts at this point and ends at this point — it
 * doesn't work that way; being able to change that, and have it show up in the
 * output, is very powerful." Edits here ride along with the next question and
 * are cited in the brief as a requester-ADJUSTED calendar.
 */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const MonthSelect: FC<{ value: number; onChange: (m: number) => void }> = ({
  value,
  onChange,
}) => (
  <select
    className="select select-xs w-16"
    value={value}
    onChange={(e) => onChange(Number(e.target.value))}
  >
    {MONTHS.map((m, i) => (
      <option key={m} value={i + 1}>
        {m}
      </option>
    ))}
  </select>
);

const title = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

const CalendarPanel: FC = () => {
  const { data } = useQuery({ queryKey: ['food-security', 'calendar'], queryFn: getCropCalendar });
  const calendarAdjust = useChatStore((s) => s.calendarAdjust);
  const setCalendarAdjust = useChatStore((s) => s.setCalendarAdjust);
  const [country, setCountry] = useState('kenya');
  const [crop, setCrop] = useState('maize');

  const countries = Object.keys(data?.calendar ?? {});
  const crops = Object.keys(data?.calendar?.[country] ?? {});
  const defaults = data?.calendar?.[country]?.[crop] ?? [];
  const seasons = calendarAdjust ?? defaults;
  const adjusted = calendarAdjust !== null;

  const edit = (si: number, field: 'planting' | 'harvest', wi: number, month: number) => {
    const next: SeasonSpec[] = seasons.map((s, i) =>
      i === si ? { ...s, [field]: s[field].map((m, j) => (j === wi ? month : m)) } : s,
    );
    // Back at the hub default -> drop the override entirely (an unchanged
    // calendar must not be presented as adjusted).
    setCalendarAdjust(JSON.stringify(next) === JSON.stringify(defaults) ? null : next);
  };

  if (!data) return null;

  return (
    <details className="collapse collapse-arrow border border-base-300 bg-base-100 rounded-lg">
      <summary className="collapse-title min-h-0 py-2 px-3 text-xs font-medium flex items-center gap-2">
        <CalendarCog className="w-3.5 h-3.5" />
        Crop calendar
        <span className={cn('badge badge-xs', adjusted ? 'badge-warning' : 'badge-ghost')}>
          {adjusted ? 'adjusted — will be cited in the brief' : 'hub default'}
        </span>
      </summary>
      <div className="collapse-content px-3 pb-2 flex flex-col gap-2 text-xs">
        <div className="flex gap-2">
          <select
            className="select select-xs"
            value={country}
            onChange={(e) => {
              setCountry(e.target.value);
              setCalendarAdjust(null);
            }}
          >
            {countries.map((c) => (
              <option key={c} value={c}>
                {title(c)}
              </option>
            ))}
          </select>
          <select
            className="select select-xs"
            value={crop}
            onChange={(e) => {
              setCrop(e.target.value);
              setCalendarAdjust(null);
            }}
          >
            {crops.map((c) => (
              <option key={c} value={c}>
                {title(c)}
              </option>
            ))}
          </select>
          {adjusted && (
            <button
              type="button"
              className="btn btn-xs btn-ghost gap-1"
              onClick={() => setCalendarAdjust(null)}
            >
              <RotateCcw className="w-3 h-3" />
              reset to hub default
            </button>
          )}
        </div>
        {seasons.map((s, si) => (
          <div key={s.season} className="flex flex-wrap items-center gap-1.5">
            <span className="font-semibold w-24">{s.season}</span>
            <span className="text-base-content/60">planting</span>
            <MonthSelect value={s.planting[0]} onChange={(m) => edit(si, 'planting', 0, m)} />
            <span>–</span>
            <MonthSelect value={s.planting[1]} onChange={(m) => edit(si, 'planting', 1, m)} />
            <span className="text-base-content/60 ml-2">harvest</span>
            <MonthSelect value={s.harvest[0]} onChange={(m) => edit(si, 'harvest', 0, m)} />
            <span>–</span>
            <MonthSelect value={s.harvest[1]} onChange={(m) => edit(si, 'harvest', 1, m)} />
          </div>
        ))}
        <p className="text-base-content/50">
          Season windows the brief's timing caveat is computed against. Changes apply to your
          next question and are cited in the brief as an adjusted calendar — never silently.
        </p>
      </div>
    </details>
  );
};

export default CalendarPanel;
