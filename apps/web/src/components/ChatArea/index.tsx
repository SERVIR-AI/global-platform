import { FC, useState } from 'react';
import { cn } from '../../lib/utils';
import InputMessage from './InputMessage';
import { AudioWaveform, Database, DropletOff, Droplets, Flame, Tornado } from 'lucide-react';

const ChatArea: FC = () => {
  const [welcome] = useState(true);
  return (
    <div className={cn('basis-1 grow flex flex-col', welcome ? 'justify-center' : 'justify-end')}>
      {welcome && (
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
            <button className="underline link">
              <Database className="inline mr-1 w-3 h-3" />
              Bring your own data
            </button>
          </div>
        </div>
      )}
      <div className={welcome ? 'px-12' : 'p-4'}>
        <InputMessage />
      </div>
    </div>
  );
};

export default ChatArea;
