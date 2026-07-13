import { FC, useEffect, useRef } from 'react';
import { useChatPending } from '@/hooks/useChat';
import { cn } from '../../lib/utils';
import { useChatStore, type UseCase } from '../../stores/ChatStore';
import ChatBubble from './ChatBubble';
import DataUpload from './DataUpload';
import InputMessage from './InputMessage';
import {
  Archive,
  AudioWaveform,
  BadgeCheck,
  DropletOff,
  Droplets,
  Flame,
  Link2,
  Tornado,
  Wheat,
} from 'lucide-react';

const USE_CASES: { value: UseCase; label: string }[] = [
  { value: 'risk', label: 'Risk' },
  { value: 'food-security', label: 'Food Security' },
];

const UseCaseSwitcher: FC = () => {
  const useCase = useChatStore((s) => s.useCase);
  const setUseCase = useChatStore((s) => s.setUseCase);
  const loading = useChatPending();
  return (
    <div className="join self-center" role="tablist">
      {USE_CASES.map(({ value, label }) => (
        <button
          key={value}
          type="button"
          role="tab"
          className={cn('btn btn-xs join-item', useCase === value ? 'btn-primary' : 'btn-ghost')}
          onClick={() => setUseCase(value)}
          disabled={loading}
        >
          {label}
        </button>
      ))}
    </div>
  );
};

const ChatArea: FC = () => {
  const messages = useChatStore((s) => s.messages);
  const useCase = useChatStore((s) => s.useCase);
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
        <div className="flex flex-col gap-3 mb-4">
          <UseCaseSwitcher />
          {useCase === 'risk' ? (
            <>
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
            </>
          ) : (
            <>
              <h1 className="px-4 text-center text-3xl font-semibold">
                Ask about crop conditions, seasonal outlooks & El Niño
              </h1>
              <p className="px-4 text-center text-sm text-zinc-500">
                Answers are written only from named reports and live expert assessments.
              </p>
              <div className="flex flex-row flex-wrap gap-3 justify-center items-center [&_span]:inline text-xs text-zinc-400">
                <span>
                  <Wheat className="inline mr-1 w-3 h-3" />
                  GEOGLAM · FEWS NET · FAO · ICPAC…
                </span>
                <span>
                  <Link2 className="inline mr-1 w-3 h-3" />
                  Every claim cited
                </span>
                <span>
                  <Archive className="inline mr-1 w-3 h-3" />
                  Originals archived
                </span>
                <span>
                  <BadgeCheck className="inline mr-1 w-3 h-3" />
                  Declines when unsure
                </span>
              </div>
            </>
          )}
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
          welcome ? 'self-center px-8 w-full max-w-2xl' : 'shrink-0 pt-1 pb-4 px-4 border-t border-zinc-200',
        )}
      >
        {!welcome && (
          <div className="flex items-center justify-between">
            <UseCaseSwitcher />
            {useCase === 'risk' && <DataUpload />}
          </div>
        )}
        <InputMessage />
      </div>
    </div>
  );
};

export default ChatArea;
