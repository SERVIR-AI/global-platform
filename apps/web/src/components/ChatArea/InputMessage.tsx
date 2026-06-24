import Dropdown from '@/components/Inputs/Dropdown';
import Spinner from '@/components/Spinner';
import { useChat, useChatPending } from '@/hooks/useChat';
import { cn } from '@/lib/utils';
import { useChatStore } from '@/stores/ChatStore';
import { DrawMode, useCustomGeometryStore } from '@/stores/CustomGeometryStore';
import { ChatProvider } from '@/types/chat';
import { ArrowRight, LucideIcon, MapPin, Paperclip, Pentagon, Square } from 'lucide-react';
import { FC, KeyboardEvent, useEffect, useRef, useState } from 'react';

const providerOptions: Record<ChatProvider, string> = {
  claude: 'Claude',
  gemini: 'Gemini',
  openai: 'OpenAI',
};

const drawButtons: { mode: DrawMode; Icon: LucideIcon; label: string }[] = [
  { mode: 'Point', Icon: MapPin, label: 'Draw a point' },
  { mode: 'Rectangle', Icon: Square, label: 'Draw a rectangle' },
  { mode: 'Polygon', Icon: Pentagon, label: 'Draw a polygon' },
];

const InputMessage: FC = () => {
  const hasMessages = useChatStore((store) => !!store.messages.length);
  const geometryType = useCustomGeometryStore((store) => store.geometryType);
  const setGeometry = useCustomGeometryStore((store) => store.setGeometry);
  const drawMode = useCustomGeometryStore((store) => store.drawMode);
  const setDrawMode = useCustomGeometryStore((store) => store.setDrawMode);
  const provider = useChatStore((store) => store.provider);
  const setProvider = useChatStore((store) => store.setProvider);

  const [text, setText] = useState('');
  const { send, isPending: loading } = useChat();
  const chatPending = useChatPending();
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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

  const getPlaceholder = (): string | undefined => {
    if (hasMessages) {
      return undefined;
    }
    if (drawMode === 'Point' || geometryType === 'Point') {
      return 'Show me schools at high risk of flooding within 5kms from this point.';
    }
    if (drawMode === 'Polygon' || drawMode === 'Rectangle' || geometryType) {
      return 'Show me schools at high risk of flooding in the area.';
    }
    return 'Show me schools at high risk of flooding in Battambang.';
  };

  useEffect(() => {
    if (chatPending) {
      return;
    }
    textareaRef.current?.focus();
  }, [chatPending]);

  return (
    <div className="flex flex-col gap-2">
      <textarea
        ref={textareaRef}
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
            disabled={loading}
          >
            <Paperclip className="w-4 h-4" />
            Attach files
          </button>
          {drawButtons.map(({ mode, Icon, label }) => {
            // A geometry of this mode is attached: this button removes it instead
            // of drawing. A geometry of a *different* mode blocks (disables) this one.
            const attached = geometryType === mode;
            const blocked = !!geometryType && !attached;
            // The tooltip sits on the wrapper since a disabled button gets no hover.
            return (
              <span
                key={mode}
                className={cn(blocked ? 'tooltip' : null)}
                data-tip="Only one geometry can be attached at a time"
              >
                <button
                  type="button"
                  className={cn(
                    'btn rounded-xl h-6 p-1 text-xs font-medium text-zinc-500',
                    attached ? 'btn-primary text-white' : null,
                    drawMode === mode ? 'btn-secondary text-white' : null,
                  )}
                  onClick={() =>
                    attached ? setGeometry(null) : setDrawMode(drawMode === mode ? null : mode)
                  }
                  disabled={loading || blocked}
                >
                  <Icon className="w-4 h-4" />
                  {attached ? `Remove the ${mode.toLowerCase()}` : label}
                </button>
              </span>
            );
          })}
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
