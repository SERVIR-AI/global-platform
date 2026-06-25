import servirLogo from '@/assets/servir.png';
import { TrafficCone } from 'lucide-react';
import { FC } from 'react';

const AppBar: FC = () => (
  <div className="w-full bg-zinc-800 text-white h-10 shrink-0 flex items-center px-4 justify-between">
    <div className="flex gap-2">
      <TrafficCone />
      <span>Global Risk Platform</span>
    </div>
    <a href="https://www.servirglobal.net/" target="_blank">
      <img src={servirLogo} alt="SERVIR" className="h-4 w-auto" />
    </a>
  </div>
);

export default AppBar;
