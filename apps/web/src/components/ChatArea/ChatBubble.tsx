import { useChat } from '@/hooks/useChat';
import { cn, getChatItemDate } from '@/lib/utils';
import type { ChatChoice, ChatItem, ChatMessage, Citation, Grounded } from '@/types/chat';
import { FC, useMemo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BriefSources, CopyBriefButton, DeclineCard, GroundedStrip, jumpToCitation } from './Brief';
import TracePanel from '../Trace/TracePanel';
import ChatMapLayer from './ChatMapLayer';

// Tailwind's preflight strips default margins/list styling, so map the elements
// that show up in assistant replies to get sensible spacing inside a bubble.
const markdownComponents: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="mb-2 last:mb-0 list-disc list-outside pl-5">{children}</ul>,
  ol: ({ children }) => (
    <ol className="mb-2 last:mb-0 list-decimal list-outside pl-5">{children}</ol>
  ),
  li: ({ children }) => <li className="mb-1 last:mb-0">{children}</li>,
  h1: ({ children }) => <h1 className="mb-2 text-lg font-semibold">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 text-base font-semibold">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-2 text-sm font-semibold">{children}</h3>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer" className="link">
      {children}
    </a>
  ),
  code: ({ className, children }) => {
    const inline = !className?.includes('language-');
    return inline ? (
      <code className="rounded bg-black/10 px-1 py-0.5 text-[0.85em]">{children}</code>
    ) : (
      <code className={cn('font-mono text-[0.85em]', className)}>{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="mb-2 last:mb-0 overflow-x-auto rounded-lg bg-black/10 p-3">{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="mb-2 last:mb-0 border-l-2 border-current/30 pl-3 italic">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="mb-2 last:mb-0 overflow-x-auto">
      <table className="table table-sm">{children}</table>
    </div>
  ),
};

const itemMessage = (item: ChatItem): ChatMessage =>
  'message' in item ? item.message : item.messages[item.messages.length - 1];

// Brief prose carries [n] markers — single, clustered [1, 9], or ranged [1-3]
// (all forms the backend gate accepts). Each cited number becomes a chip link
// (`cite:` protocol); bracketed years stay plain text. The markdown's own
// "## Sources" block is dropped from display — the structured cards replace it
// (the full text, sources included, remains behind the copy button).
const briefBody = (markdown: string): string =>
  markdown.split(/\n## Sources\b/)[0].replace(/\[([\d\s,\-–]{1,24})\]/g, (match, group: string) => {
    const nums = new Set<number>();
    for (const part of group.replace(/–/g, '-').split(/[\s,]+/)) {
      const range = part.match(/^(\d+)-(\d+)$/);
      if (range) {
        const a = Number(range[1]);
        const b = Math.min(Number(range[2]), a + 50);
        for (let n = a; n <= b; n++) nums.add(n);
      } else if (/^\d+$/.test(part)) {
        nums.add(Number(part));
      }
    }
    const cites = [...nums].filter((n) => n > 0 && n < 1000).sort((a, b) => a - b);
    if (cites.length === 0) return match; // e.g. [2016] — a year, not a citation
    return cites.map((n) => `[**[${n}]**](cite:${n})`).join('');
  });

// The agent's exposure-vs-risk question comes back with `choices`; render them as
// buttons that send the option's `value` (e.g. "1") as the next reply on this thread.
const ChoiceButtons: FC<{ choices: ChatChoice[] }> = ({ choices }) => {
  const { send, isPending } = useChat();
  return (
    <div className="flex flex-wrap gap-2">
      {choices.map((choice) => (
        <button
          key={choice.value}
          type="button"
          className="btn btn-sm btn-outline btn-primary"
          disabled={isPending}
          onClick={() => send(choice.value)}
        >
          {choice.label}
        </button>
      ))}
    </div>
  );
};

const ChatBubble: FC<{ chatItem: ChatItem }> = ({ chatItem }) => {
  const message = itemMessage(chatItem);
  const isUser = message.role === 'user';
  const date = getChatItemDate(chatItem);
  const choices = (chatItem as { choices?: ChatChoice[] | null }).choices;

  // Food-security brief fields (absent on risk turns).
  const brief = (chatItem as { brief?: string | null }).brief;
  const citations = (chatItem as { citations?: Citation[] }).citations ?? [];
  const grounded = (chatItem as { grounded?: Grounded | null }).grounded;
  const declined = (chatItem as { declined?: boolean }).declined === true;
  const model = (chatItem as { model?: string }).model;
  const bubbleId = useMemo(() => ('id' in chatItem ? String(chatItem.id) : ''), [chatItem]);
  const isBrief = !isUser && !declined && !!brief;

  const briefComponents: Components = useMemo(
    () => ({
      ...markdownComponents,
      a: ({ children, href }) => {
        const cite = href?.startsWith('cite:') ? Number(href.slice(5)) : null;
        if (cite !== null)
          return (
            <button
              type="button"
              className="align-super text-[0.7em] font-semibold text-primary hover:underline"
              title="Jump to this source"
              onClick={() => jumpToCitation(bubbleId, cite)}
            >
              {children}
            </button>
          );
        return (
          <a href={href} target="_blank" rel="noreferrer" className="link">
            {children}
          </a>
        );
      },
    }),
    [bubbleId],
  );

  return (
    <div className={cn('flex flex-col gap-2', isUser ? 'items-end' : 'items-start')}>
      <div
        className={cn(
          'w-fit rounded-xl',
          isBrief ? 'max-w-full' : 'max-w-[90%]',
          !isUser && declined ? 'p-0' : 'px-4 py-2',
          isUser
            ? 'bg-primary text-primary-content whitespace-pre-wrap'
            : declined
              ? ''
              : 'bg-base-300 text-base-content',
        )}
      >
        {isUser ? (
          message.content
        ) : declined ? (
          <DeclineCard reason={message.content} />
        ) : isBrief ? (
          <div className="flex flex-col gap-2">
            <GroundedStrip grounded={grounded!} citations={citations} model={model} />
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={briefComponents}>
              {briefBody(brief!)}
            </ReactMarkdown>
            <BriefSources citations={citations} bubbleId={bubbleId} />
            <div className="self-end">
              <CopyBriefButton brief={brief!} grounded={grounded} model={model} />
            </div>
          </div>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {message.content}
          </ReactMarkdown>
        )}
      </div>
      {!isUser && choices && choices.length > 0 && (
        <ChoiceButtons choices={choices} />
      )}
      <div className="flex flex-col gap-1">
        {chatItem.layers.map((layer, index) => (
          <ChatMapLayer key={index} layer={layer} />
        ))}
        {!isUser && <TracePanel item={chatItem} />}
        {date && (
          <span className={cn('text-zinc-400 text-xs', isUser ? 'self-end' : undefined)}>
            {date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}
      </div>
    </div>
  );
};

export default ChatBubble;
