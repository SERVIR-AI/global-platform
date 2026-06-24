import Dropdown from '@/components/Inputs/Dropdown';
import Spinner from '@/components/Spinner';
import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import { useCustomGeometryStore } from '@/stores/CustomGeometryStore';
import { ChatProvider } from '@/types/chat';
import { ArrowRight, MapPin, MapPinCheck, Paperclip, Pentagon, Square, X } from 'lucide-react';
import { FC, KeyboardEvent, useState } from 'react';

const providerOptions: Record<ChatProvider, string> = {
  claude: 'Claude',
  gemini: 'Gemini',
  openai: 'OpenAI',
};

const InputMessage: FC = () => {
  const geometry = useCustomGeometryStore((store) => store.geometry);
  const setGeometry = useCustomGeometryStore((store) => store.setGeometry);
  const drawMode = useCustomGeometryStore((store) => store.drawMode);
  const setDrawMode = useCustomGeometryStore((store) => store.setDrawMode);
  const provider = useChatStore((store) => store.provider);
  const setProvider = useChatStore((store) => store.setProvider);

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
        className={cn(
          'border border-zinc-400 rounded-xl w-full resize-none p-2',
          loading ? ' bg-zinc-100' : undefined,
        )}
        placeholder={getPlaceholder()}
        disabled={loading}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="flex flex-col md:flex-row justify-between gap-4">
        <div className="flex flex-row flex-wrap gap-2">
          <button
            type="button"
            className="btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500 tooltip"
            data-tip="Coming Soon"
          >
            <Paperclip className="w-4 h-4" />
            Attach files
          </button>
          {geometry ? (
            <div className="badge badge-secondary badge-sm h-6 rounded-xl">
              <MapPinCheck className="w-3 h-3" />
              Geometry Attached
              {!loading && (
                <button
                  className="p-0 cursor-pointer tooltip"
                  data-tip="Remove geometry"
                  onClick={() => setGeometry(null)}
                >
                  <X className="w-3 h-3" />
                </button>
              )}
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
                disabled={loading}
              >
                <MapPin className="w-4 h-4" />
                Draw a point
              </button>
              <button
                type="button"
                className={cn(
                  'btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500',
                  drawMode === 'Rectangle' ? 'btn-secondary text-white' : null,
                )}
                onClick={() => setDrawMode(drawMode === 'Rectangle' ? null : 'Rectangle')}
                disabled={loading}
              >
                <Square className="w-4 h-4" />
                Draw a rectangle
              </button>
              <button
                type="button"
                className={cn(
                  'btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500',
                  drawMode === 'Polygon' ? 'btn-secondary text-white' : null,
                )}
                onClick={() => setDrawMode(drawMode === 'Polygon' ? null : 'Polygon')}
                disabled={loading}
              >
                <Pentagon className="w-4 h-4" />
                Draw a polygon
              </button>
            </>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Dropdown<ChatProvider>
            value={provider}
            setValue={setProvider}
            options={providerOptions}
            disabled={loading}
          />
          <button
            type="button"
            className="btn btn-primary rounded-full w-8 h-8 p-2"
            onClick={submit}
            disabled={loading || !text.trim()}
          >
            {loading ? (
              <Spinner className="loading-xs text-primary" />
            ) : (
              <ArrowRight className="w-4 h-4" />
            )}
          </button>
        </div>
      </div>
    </div>
  );
};

export default InputMessage;
