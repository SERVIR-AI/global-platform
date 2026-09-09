import AuthControl from '@/components/AuthControl';
import servirLogo from '@/assets/servir.png';
import { TrafficCone } from 'lucide-react';
import { FC } from 'react';

const AppBar: FC = () => (
  <div className="w-full bg-zinc-800 text-white h-10 shrink-0 flex items-center px-4 justify-between gap-4">
    <div className="flex items-center gap-3 min-w-0">
      <div className="flex gap-2 shrink-0">
        <TrafficCone />
        <span>Global Risk Platform</span>
      </div>
      <a
        href="https://www.servirglobal.net/"
        target="_blank"
        className="shrink-0 flex items-center"
      >
        <img src={servirLogo} alt="SERVIR" className="h-4 w-auto" />
      </a>
    </div>
    <AuthControl />
  </div>
);

export default AppBar;
