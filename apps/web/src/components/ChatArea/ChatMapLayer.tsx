import { useMapLayer } from '@/hooks/useMapLayer';
import { cn } from '@/lib/utils';
import { ChatLayer } from '@/types/chat';
import {
  ArrowLeftFromLine,
  ArrowRightToLine,
  Check,
  Circle,
  CircleDashed,
  Fullscreen,
  LandPlot,
  LayoutList,
  MapPin,
  Pentagon,
  Save,
  Spline,
  Square,
  SquaresExclude,
} from 'lucide-react';
import { FC, useState } from 'react';

type ChatMapLayerProps = {
  layer: ChatLayer;
};

const ChatMapLayer: FC<ChatMapLayerProps> = ({ layer }) => {
  const {
    available,
    shown,
    opacity,
    setOpacity,
    toggle,
    zoomTo,
    download,
    bringToFront,
    bringToBack,
  } = useMapLayer(layer);
  const [symbologyShown, setSymbologyShown] = useState(false);
  const hasSymbology = layer.symbology.length > 0;
  // `z-20` lifts the button (and its tooltip) above the map column, which sits
  // later in the DOM and would otherwise paint over the tooltip.

  const downloadButton = (
    <button
      className="join-item btn btn-xs px-1 tooltip z-20"
      data-tip={layer.type === 'Raster' ? 'Save layer as .tif' : 'Save layer as .geojson'}
      onClick={download}
    >
      <Save className="w-4 h-4" />
    </button>
  );

  return (
    <div className="flex flex-col gap-0">
      <div className="join">
        <button
          className="join-item btn btn-xs tooltip z-20"
          data-tip={shown ? 'Remove from map' : 'Add to map'}
          onClick={toggle}
          disabled={!available}
          title={layer.description}
        >
          {shown ? <Check className="w-4 h-4" /> : <div className="w-4 h-4" />}
          {layer.type === 'Point' && <MapPin className="w-4 h-4" />}
          {layer.type === 'LineString' && <Spline className="w-4 h-4" />}
          {layer.type === 'Polygon' && <Pentagon className="w-4 h-4" />}
          {layer.type === 'Rectangle' && <Square className="w-4 h-4" />}
          {layer.type === 'Raster' && <LandPlot className="w-4 h-4" />}
          {layer.type === 'Vector' && <SquaresExclude className="w-4 h-4" />}
          {layer.name}
          <small>({layer.type})</small>
        </button>
        {shown ? (
          <>
            <button
              className="join-item btn btn-xs px-1 tooltip z-20"
              data-tip="Bring to back"
              onClick={bringToBack}
            >
              <ArrowLeftFromLine className="w-4 h-4" />
            </button>
            <button
              className="join-item btn btn-xs px-1 tooltip z-20"
              data-tip="Bring to front"
              onClick={bringToFront}
            >
              <ArrowRightToLine className="w-4 h-4" />
            </button>
            {hasSymbology && (
              <button
                className={cn(
                  'join-item btn btn-xs px-1 tooltip z-20',
                  symbologyShown ? 'btn-active' : undefined,
                )}
                data-tip={symbologyShown ? 'Hide symbology' : 'Show symbology'}
                onClick={() => setSymbologyShown((v) => !v)}
              >
                <LayoutList className="w-4 h-4" />
              </button>
            )}
            {opacity < 1 ? (
              <button
                className="join-item btn btn-xs px-1 tooltip z-20"
                data-tip="Remove opacity"
                onClick={() => setOpacity(1)}
              >
                <CircleDashed className="w-4 h-4" />
              </button>
            ) : (
              <button
                className="join-item btn btn-xs px-1 tooltip z-20"
                data-tip="Add opacity"
                onClick={() => setOpacity(0.5)}
              >
                <Circle className="w-4 h-4" />
              </button>
            )}
            {downloadButton}
            <button
              className="join-item btn btn-xs px-1 tooltip z-20"
              data-tip="Zoom to layer"
              onClick={zoomTo}
            >
              <Fullscreen className="w-4 h-4" />
            </button>
          </>
        ) : (
          downloadButton
        )}
      </div>
      {shown && symbologyShown && hasSymbology && (
        <div className="flex flex-col gap-0.5 my-1 ml-2">
          {layer.symbology.map((entry) => (
            <div key={entry.label} className="flex flex-row gap-1 text-xs items-center">
              <div
                className="w-4 h-3 rounded border border-black/10"
                style={{ backgroundColor: entry.color }}
              />
              <small>{entry.label}</small>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ChatMapLayer;
