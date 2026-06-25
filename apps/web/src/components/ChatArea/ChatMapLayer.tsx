import { useMapLayer } from '@/hooks/useMapLayer';
import { ChatLayer } from '@/types/chat';
import {
  ArrowLeftFromLine,
  ArrowRightToLine,
  Check,
  Circle,
  CircleDashed,
  Fullscreen,
  LandPlot,
  MapPin,
  Pentagon,
  Spline,
  Square,
  SquaresExclude,
} from 'lucide-react';
import { FC } from 'react';

type ChatMapLayerProps = {
  layer: ChatLayer;
};

const ChatMapLayer: FC<ChatMapLayerProps> = ({ layer }) => {
  const { available, shown, opacity, setOpacity, toggle, zoomTo, bringToFront, bringToBack } =
    useMapLayer(layer);
  // `z-20` lifts the button (and its tooltip) above the map column, which sits
  // later in the DOM and would otherwise paint over the tooltip.
  return (
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
      {shown && (
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
          {opacity < 1 ? (
            <button
              className="join-item btn btn-xs px-1 tooltip z-20"
              data-tip="Add opacity"
              onClick={() => setOpacity(1)}
            >
              <Circle className="w-4 h-4" />
            </button>
          ) : (
            <button
              className="join-item btn btn-xs px-1 tooltip z-20"
              data-tip="Remove opacity"
              onClick={() => setOpacity(0.5)}
            >
              <CircleDashed className="w-4 h-4" />
            </button>
          )}
          <button
            className="join-item btn btn-xs px-1 tooltip z-20"
            data-tip="Zoom to layer"
            onClick={zoomTo}
          >
            <Fullscreen className="w-4 h-4" />
          </button>
        </>
      )}
    </div>
  );
};

export default ChatMapLayer;
