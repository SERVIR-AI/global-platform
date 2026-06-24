import { cn } from '@/lib/utils';
import { ChevronDown } from 'lucide-react';
import { useRef, useState } from 'react';

interface DropdownProps<T extends string> {
  value: T;
  setValue: (v: T) => void;
  options: Record<T, string>;
  className?: string;
  disabled?: boolean;
}

const Dropdown = <T extends string>({
  value,
  setValue,
  options,
  className,
  disabled,
}: DropdownProps<T>) => {
  const triggerRef = useRef<HTMLDivElement>(null);
  const [openUp, setOpenUp] = useState(false);

  // Flip the menu above the trigger when there isn't enough room below it.
  const updateDirection = (): void => {
    const trigger = triggerRef.current;
    const menu = trigger?.nextElementSibling;
    if (!trigger || !menu) return;
    const spaceBelow = window.innerHeight - trigger.getBoundingClientRect().bottom;
    setOpenUp(spaceBelow < menu.scrollHeight);
  };

  return (
    <div className={cn('dropdown', openUp && 'dropdown-top', disabled && 'pointer-events-none', className)}>
      <div
        ref={triggerRef}
        tabIndex={disabled ? -1 : 0}
        role="button"
        aria-disabled={disabled}
        className={cn('btn btn-xs m-1', disabled && 'btn-disabled')}
        onFocus={updateDirection}
        onMouseEnter={updateDirection}
      >
        {options[value]}
        <ChevronDown size={12} />
      </div>
      <ul
        tabIndex={-1}
        className="dropdown-content menu bg-base-100 rounded-box z-1 w-52 p-2 shadow-sm"
      >
        {(Object.keys(options) as T[]).map((option) => (
          <li key={option}>
            <a
              className={cn(option === value ? 'menu-active' : null)}
              onClick={() => {
                setValue(option);
                (document.activeElement as HTMLElement | null)?.blur();
              }}
            >
              {options[option]}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
};

export default Dropdown;
