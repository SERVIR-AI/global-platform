import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import { useCustomGeometryStore } from '@/stores/CustomGeometryStore';
import { ArrowRight, MapPin, MapPinCheck, Pentagon, Square, X } from 'lucide-react';
import { FC, KeyboardEvent, useState } from 'react';

const InputMessage: FC = () => {
  const geometry = useCustomGeometryStore((store) => store.geometry);
  const setGeometry = useCustomGeometryStore((store) => store.setGeometry);
  const drawMode = useCustomGeometryStore((store) => store.drawMode);
  const setDrawMode = useCustomGeometryStore((store) => store.setDrawMode);

  const [text, setText] = useState('');
  const send = useChatStore((store) => store.send);
  const loading = useChatStore((store) => store.loading);

  const submit = () => {
    const value = text;
    setText('');
    void send(value);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const getPlaceholder = (): string => {
    if (drawMode === 'Point' || geometry?.getType() === 'Point') {
      return 'Show me schools at high risk of flooding within 5kms from this point.';
    }
    if (drawMode === 'Polygon' || drawMode === 'Rectangle' || geometry?.getType() === 'Polygon') {
      return 'Show me schools at high risk of flooding in the area.';
    }
    return 'Show me schools at high risk of flooding in Battambang.';
  };

  return (
    <div className="flex flex-col gap-2">
      <textarea
        className="border border-zinc-400 rounded-xl w-full resize-none p-2"
        placeholder={getPlaceholder()}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="flex justify-between">
        <div className="flex flex-row flex-wrap gap-2">
          {geometry ? (
            <div className="badge badge-secondary badge-sm">
              <MapPinCheck className="w-3 h-3" />
              Geometry Attached
              <div className="tooltip flex" data-tip="Remove geometry">
                <button className="p-0 cursor-pointer" onClick={() => setGeometry(null)}>
                  <X className="w-3 h-3" />
                </button>
              </div>
            </div>
          ) : (
            <>
              <button
                type="button"
                className={cn(
                  'btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500',
                  drawMode === 'Point' ? 'btn-secondary text-white' : null,
                )}
                onClick={() => setDrawMode(drawMode === 'Point' ? null : 'Point')}
              >
                <MapPin className="w-4 h-4" />
                Draw a Point
              </button>
              <button
                type="button"
                className={cn(
                  'btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500',
                  drawMode === 'Rectangle' ? 'btn-secondary text-white' : null,
                )}
                onClick={() => setDrawMode(drawMode === 'Rectangle' ? null : 'Rectangle')}
              >
                <Square className="w-4 h-4" />
                Draw a Rectangle
              </button>
              <button
                type="button"
                className={cn(
                  'btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500',
                  drawMode === 'Polygon' ? 'btn-secondary text-white' : null,
                )}
                onClick={() => setDrawMode(drawMode === 'Polygon' ? null : 'Polygon')}
              >
                <Pentagon className="w-4 h-4" />
                Draw a Polygon
              </button>
            </>
          )}
        </div>
        <button
          type="button"
          className="btn btn-primary rounded-full w-8 h-8 p-2"
          onClick={submit}
          disabled={loading || !text.trim()}
        >
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
};

export default InputMessage;
