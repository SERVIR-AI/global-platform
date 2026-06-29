import { cn } from '@/lib/utils';
import { UseUserInterfaceStore } from '@/stores/UserInterfaceStore';
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, type LucideIcon } from 'lucide-react';
import { FC } from 'react';

interface DividerButtonProps {
  /** Icon shown on mobile (column layout: up/down). */
  MobileIcon: LucideIcon;
  /** Icon shown on desktop (row layout: left/right). */
  DesktopIcon: LucideIcon;
  title: string;
  hide: boolean;
  onClick: () => void;
}

const DividerButton: FC<DividerButtonProps> = ({
  MobileIcon,
  DesktopIcon,
  title,
  hide,
  onClick,
}) => (
  <button
    type="button"
    title={title}
    onClick={onClick}
    className={cn('btn btn-xs rounded-none p-0 h-4 lg:h-8 w-12 lg:w-4', hide && 'hidden')}
  >
    <MobileIcon className="w-3 h-3 lg:hidden" />
    <DesktopIcon className="w-3 h-3 hidden lg:block" />
  </button>
);

const SectionDivider: FC = () => {
  const chatExpanded = UseUserInterfaceStore((store) => store.chatExpanded);
  const mapsExpanded = UseUserInterfaceStore((store) => store.mapsExpanded);
  const setChatExpanded = UseUserInterfaceStore((store) => store.setChatExpanded);
  const setMapsExpanded = UseUserInterfaceStore((store) => store.setMapsExpanded);

  return (
    <div className="group relative self-stretch h-0 w-full lg:h-auto lg:w-0">
      {/* Expanded hover target: widens the area that reveals the buttons on pointer devices. */}
      <div className="absolute w-full h-12 -top-6 left-0 lg:w-12 lg:top-0 lg:-left-6 lg:h-full z-10" />
      <div
        className={cn(
          'absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-10 flex flex-col-reverse lg:flex-row',
          // Clip the group's outer corners instead of using daisyUI `join`, whose
          // first/last-child rounding fights the reversed visual order of flex-col-reverse.
          'rounded shadow overflow-hidden',
          // On pointer devices (mouse): hidden until the divider area is hovered.
          // On touch devices (no hover): always visible.
          '[@media(hover:hover)]:opacity-0 [@media(hover:hover)]:pointer-events-none',
          '[@media(hover:hover)]:group-hover:opacity-100 [@media(hover:hover)]:group-hover:pointer-events-auto',
        )}
      >
        <DividerButton
          title={chatExpanded ? 'Collapse chat' : 'Expand chat'}
          MobileIcon={chatExpanded ? ChevronDown : ChevronUp}
          DesktopIcon={chatExpanded ? ChevronLeft : ChevronRight}
          hide={chatExpanded && !mapsExpanded}
          onClick={() => setChatExpanded(!chatExpanded)}
        />
        <DividerButton
          title={mapsExpanded ? 'Collapse map' : 'Expand map'}
          MobileIcon={mapsExpanded ? ChevronUp : ChevronDown}
          DesktopIcon={mapsExpanded ? ChevronRight : ChevronLeft}
          hide={mapsExpanded && !chatExpanded}
          onClick={() => setMapsExpanded(!mapsExpanded)}
        />
      </div>
    </div>
  );
};

export default SectionDivider;
