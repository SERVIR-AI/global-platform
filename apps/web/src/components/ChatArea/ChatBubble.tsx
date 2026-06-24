import { cn } from '@/lib/utils';
import type { ChatMessage } from '@/types/chat';
import { FC } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

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

const ChatBubble: FC<{ message: ChatMessage }> = ({ message }) => {
  const isUser = message.role === 'user';
  return (
    <div className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={cn(
          'w-fit max-w-[90%] rounded-xl px-4 py-2',
          isUser
            ? 'bg-primary text-primary-content whitespace-pre-wrap'
            : 'bg-base-300 text-base-content',
        )}
      >
        {isUser ? (
          message.content
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {message.content}
          </ReactMarkdown>
        )}
      </div>
    </div>
  );
};

export default ChatBubble;
