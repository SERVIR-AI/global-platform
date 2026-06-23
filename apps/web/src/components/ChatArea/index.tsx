import { FC, useState } from 'react';
import { cn } from '../../lib/utils';
import InputMessage from './InputMessage';

const ChatArea: FC = () => {
  const [welcome] = useState(true);
  return (
    <div className={cn('basis-1 grow flex flex-col', welcome ? 'justify-center' : 'justify-end')}>
      {welcome && (
        <h1 className="px-4 pb-4 text-center text-3xl font-semibold mb-2">
          What risk analysis do you want to visualize?
        </h1>
      )}
      <div className={welcome ? 'px-12' : 'p-4'}>
        <InputMessage />
      </div>
    </div>
  );
};

export default ChatArea;
