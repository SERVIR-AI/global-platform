import { FC, useEffect, useRef } from 'react';
import { useChatPending } from '@/hooks/useChat';
import { cn } from '../../lib/utils';
import { useChatStore } from '../../stores/ChatStore';
import ChatBubble from './ChatBubble';
import DataUpload from './DataUpload';
import InputMessage from './InputMessage';
import { AudioWaveform, DropletOff, Droplets, Flame, Tornado } from 'lucide-react';

const ChatArea: FC = () => {
  const messages = useChatStore((s) => s.messages);
  const loading = useChatPending();
  const welcome = messages.length === 0;

  // Scroll to the bottom on every new message — both when the user sends and
  // when the API response arrives.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scrollRef.current;
    el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  return (
    <div
      className={cn(
        'basis-1 grow flex flex-col min-h-0',
        welcome ? 'justify-center' : 'justify-end',
      )}
    >
      {welcome ? (
        <div className="flex flex-col gap-2 mb-4">
          <h1 className="px-4 text-center text-3xl font-semibold">
            What risk analysis do you want to visualize?
          </h1>
          <div className="flex flex-row flex-wrap gap-3 justify-center items-center [&_span]:inline text-xs text-zinc-400">
            <span>
              <Droplets className="inline mr-1 w-3 h-3" />
              Flood
            </span>
            <span>
              <Tornado className="inline mr-1 w-3 h-3" />
              Cyclone
            </span>
            <span>
              <AudioWaveform className="inline mr-1 w-3 h-3" />
              Earthquake
            </span>
            <span>
              <DropletOff className="inline mr-1 w-3 h-3" />
              Drought
            </span>
            <span>
              <Flame className="inline mr-1 w-3 h-3" />
              Fire
            </span>
            <DataUpload />
          </div>
        </div>
      ) : (
        <div
          ref={scrollRef}
          className="flex-1 min-h-0 overflow-y-auto py-4 px-8 flex flex-col gap-3"
        >
          {messages.map((m, i) => (
            <ChatBubble key={i} chatItem={m} />
          ))}
          {loading && (
            <div className="flex justify-start">
              <div className="w-fit rounded-xl px-4 py-2 bg-base-300">
                <span className="loading loading-dots loading-sm" />
              </div>
            </div>
          )}
        </div>
      )}
      <div
        className={cn(
          'flex flex-col gap-2',
          welcome ? 'self-center px-8' : 'shrink-0 pt-1 pb-4 px-4 border-t border-zinc-200',
        )}
      >
        {!welcome && (
          <div className="self-end">
            <DataUpload />
          </div>
        )}
        <InputMessage />
      </div>
    </div>
  );
};

export default ChatArea;
