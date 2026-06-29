import { useCallback } from 'react';
import { isEmpty, type Extent } from 'ol/extent';
import type BaseLayer from 'ol/layer/Base';
import type Layer from 'ol/layer/Layer';
import VectorSource from 'ol/source/Vector';
import { useChatStore } from '@/stores/ChatStore';
import { useMapStore } from '@/stores/MapStore';
import type { ChatLayer } from '@/types/chat';

const FIT_OPTIONS = { padding: [40, 40, 40, 40], duration: 300, maxZoom: 16 };

/** The layer's extent in the view projection, or undefined when it has none. */
const layerExtent = (layer: Layer): Extent | undefined => {
  const source = layer.getSource();
  if (source instanceof VectorSource) {
    const extent = source.getExtent();
    return !extent || isEmpty(extent) ? undefined : extent;
  }
  // Raster layers carry their extent, set from the response bounds at build time.
  return layer.getExtent();
};

/**
 * Bind a {@link ChatLayer} to the shared OpenLayers map. Visibility is state —
 * `toggle` flips the layer's `visible` flag and the map reconciler (in `Maps`)
 * brings the map into line, so the flag stays the single source of truth even
 * across map teardown/rebuild.
 *
 * Returns whether a map is `available`, whether the layer is `shown`, its
 * `opacity` plus `setOpacity`, `toggle` (show/hide), `zoomTo` (fit the view to
 * its extent), and `bringToFront` / `bringToBack` (reorder it within the chat
 * layers).
 */
export const useMapLayer = (chatLayer: ChatLayer) => {
  const map = useMapStore((s) => s.map);
  const toggleLayer = useChatStore((s) => s.toggleLayer);
  const setLayerOpacity = useChatStore((s) => s.setLayerOpacity);

  const toggle = useCallback(() => toggleLayer(chatLayer), [toggleLayer, chatLayer]);

  const setOpacity = useCallback(
    (opacity: number) => setLayerOpacity(chatLayer, opacity),
    [setLayerOpacity, chatLayer],
  );

  const zoomTo = useCallback(() => {
    const extent = map ? layerExtent(chatLayer.layer) : undefined;
    if (map && extent) map.getView().fit(extent, FIT_OPTIONS);
  }, [map, chatLayer]);

  const bringToFront = useCallback(() => {
    if (!map) return;
    const collection = map.getLayers();
    collection.remove(chatLayer.layer);
    collection.push(chatLayer.layer);
  }, [map, chatLayer]);

  const bringToBack = useCallback(() => {
    if (!map) return;
    const collection = map.getLayers();
    // Keep chat layers above the base layers (basemap + drawn geometry), which
    // belong to no chat turn — drop it just above them rather than to index 0.
    const managed = new Set<BaseLayer>(
      useChatStore.getState().messages.flatMap((m) => m.layers).map((l) => l.layer),
    );
    const baseCount = collection.getArray().filter((l) => !managed.has(l)).length;
    collection.remove(chatLayer.layer);
    collection.insertAt(baseCount, chatLayer.layer);
  }, [map, chatLayer]);

  return {
    available: !!map,
    shown: chatLayer.visible,
    opacity: chatLayer.opacity,
    setOpacity,
    toggle,
    zoomTo,
    bringToFront,
    bringToBack,
  };
};
