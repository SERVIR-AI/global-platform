/**
 * Store-free hazard map for embed mode — fed ONLY by what the platform recorded
 * on the receipt's evidence pack (`pack.viz`), resolved at view time.
 *
 * Reuses buildChatLayers, which constructs OpenLayers layers purely from
 * {aoi, hazard_layer.raster_url, bounds, legend, features}. No zustand, no chat
 * state: an embed lives on someone else's page.
 */
import { FC, useEffect, useRef } from 'react';
import Map from 'ol/Map';
import View from 'ol/View';
import TileLayer from 'ol/layer/Tile';
import OSM from 'ol/source/OSM';
import { transformExtent } from 'ol/proj';
import { buildChatLayers } from '@/lib/chatLayers';
import type { ChatResponse } from '@/types/chat';
import 'ol/ol.css';

/** The slice of ChatResponse that pack.viz carries (same field names by design). */
export type VizPayload = Pick<
  ChatResponse,
  'place' | 'hazard' | 'layer' | 'metric' | 'legend' | 'bounds' | 'aoi' | 'features' | 'hazard_layer'
>;

const EmbedMap: FC<{ viz: VizPayload }> = ({ viz }) => {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!host.current) return;
    // buildChatLayers type-narrows on `message` to tell responses from requests;
    // viz is response-shaped, so present it as one.
    const layers = buildChatLayers({ ...viz, message: {} } as unknown as ChatResponse);
    const map = new Map({
      target: host.current,
      layers: [new TileLayer({ source: new OSM() }), ...layers.map((l) => l.layer)],
      view: new View({ center: [0, 0], zoom: 2 }),
      controls: [],
    });
    if (viz.bounds)
      map.getView().fit(transformExtent(viz.bounds, 'EPSG:4326', 'EPSG:3857'), {
        padding: [24, 24, 24, 24],
        maxZoom: 14,
      });
    return () => map.setTarget(undefined);
  }, [viz]);

  const legend = viz.legend ?? {};
  return (
    <div className="flex h-full w-full flex-col">
      <div className="px-3 py-2 text-sm">
        <span className="font-semibold">{viz.place ?? 'Area of interest'}</span>
        {viz.hazard && <span className="opacity-70"> · {viz.hazard.replace('hazard_', '')} hazard</span>}
        {viz.metric?.value != null && (
          <span className="opacity-70">
            {' '}
            · {viz.metric.value} {viz.metric.unit === 'km' ? 'km of roads' : viz.layer} exposed
          </span>
        )}
      </div>
      <div ref={host} className="min-h-0 w-full flex-1" />
      {Object.keys(legend).length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 px-3 py-2 text-xs">
          {Object.entries(legend).map(([cls, { label, color }]) => (
            <span key={cls} className="inline-flex items-center gap-1">
              <span className="inline-block h-3 w-3 rounded-sm" style={{ background: color }} />
              {cls}: {label}
            </span>
          ))}
        </div>
      )}
      <div className="px-3 pb-2 text-[11px] opacity-60">
        Rendered from the receipt's recorded evidence pack — resolved live, not frozen.
      </div>
    </div>
  );
};

export default EmbedMap;
