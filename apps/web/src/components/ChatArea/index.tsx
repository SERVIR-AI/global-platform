import { FC } from 'react';
import { cn } from '../../lib/utils';
import { useChatStore } from '../../stores/ChatStore';
import InputMessage from './InputMessage';
import { AudioWaveform, Database, DropletOff, Droplets, Flame, Tornado } from 'lucide-react';

const BringYourOwnDataButton: FC = () => (
  <button className="underline link tooltip text-xs text-zinc-400" data-tip="Coming Soon">
    <Database className="inline mr-1 w-3 h-3" />
    Bring your own data
  </button>
);

const ChatArea: FC = () => {
  const messages = useChatStore((s) => s.messages);
  const loading = useChatStore((s) => s.loading);
  const welcome = messages.length === 0;

  return (
    <div className={cn('basis-1 grow flex flex-col min-h-0', welcome ? 'justify-center' : 'justify-end')}>
      {welcome ? (
        <div className="flex flex-col gap-2 mb-4">
          <h1 className="px-4 text-center text-3xl font-semibold">
            What risk analysis do you want to visualize?
          </h1>
          <div className="flex flex-row gap-3 justify-center items-center [&_span]:inline text-xs text-zinc-400">
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
            <BringYourOwnDataButton />
          </div>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">
          {messages.map((m, i) => (
            <div key={i} className={cn('chat', m.role === 'user' ? 'chat-end' : 'chat-start')}>
              <div
                className={cn(
                  'chat-bubble whitespace-pre-wrap',
                  m.role === 'user' ? 'chat-bubble-primary' : 'chat-bubble-neutral',
                )}
              >
                {m.content}
              </div>
            </div>
          ))}
          {loading && (
            <div className="chat chat-start">
              <div className="chat-bubble chat-bubble-neutral">
                <span className="loading loading-dots loading-sm" />
              </div>
            </div>
          )}
        </div>
      )}
      <div
        className={cn(
          'flex flex-col gap-2',
          welcome ? 'px-12' : 'pt-1 pb-4 px-4 border-t border-zinc-200',
        )}
      >
        {!welcome && (
          <div className="self-end">
            <BringYourOwnDataButton />
          </div>
        )}
        <InputMessage />
      </div>
    </div>
  );
};

export default ChatArea;
