import { TrafficCone } from 'lucide-react';
import { FC } from 'react';

const AppBar: FC = () => (
  <div className="w-full bg-zinc-800 text-white h-10 flex items-center px-4 gap-2">
    <TrafficCone />
    <span>Global Risk Platform</span>
  </div>
);

export default AppBar;
