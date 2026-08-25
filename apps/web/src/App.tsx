import type { FC } from 'react';
import AppBar from './components/AppBar';
import ChatArea from './components/ChatArea';
import Maps from './components/Maps';
import ProvenancePanel from './components/Provenance';
import SectionDivider from './components/SectionDivider';
import { useChatStore } from './stores/ChatStore';
import { UseUserInterfaceStore } from './stores/UserInterfaceStore';

const App: FC = () => {
  const chatExpanded = UseUserInterfaceStore((store) => store.chatExpanded);
  const mapsExpanded = UseUserInterfaceStore((store) => store.mapsExpanded);
  const useCase = useChatStore((store) => store.useCase);

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <AppBar />
      <div className="flex flex-col-reverse lg:flex-row grow min-h-0">
        {chatExpanded && <ChatArea />}
        <SectionDivider />
        {/* Food-security mode: the map panel becomes the provenance graph (until
            the supporting conditions map lands, when these become tabs). */}
        {mapsExpanded && (useCase === 'food-security' ? <ProvenancePanel /> : <Maps />)}
      </div>
    </div>
  );
};

export default App;
