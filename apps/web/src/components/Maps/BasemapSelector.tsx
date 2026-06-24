import { cn } from '@/lib/utils';
import { useMapStore } from '@/stores/MapStore';
import { FC } from 'react';

const BasemapSelector: FC = () => {
  const basemap = useMapStore((store) => store.basemap);
  const setBasemap = useMapStore((store) => store.setBasemap);

  return (
    <div className="join">
      <button
        type="button"
        className={cn('join-item btn btn-xs', basemap === 'Street' ? 'btn-primary' : undefined)}
        onClick={() => setBasemap('Street')}
      >
        Street
      </button>
      <button
        type="button"
        className={cn('join-item btn btn-xs', basemap === 'Satellite' ? 'btn-primary' : undefined)}
        onClick={() => setBasemap('Satellite')}
      >
        Satellite
      </button>
    </div>
  );
};

export default BasemapSelector;
