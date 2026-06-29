import type { FC } from 'react';
import AppBar from './components/AppBar';
import ChatArea from './components/ChatArea';
import Maps from './components/Maps';
import SectionDivider from './components/SectionDivider';
import { UseUserInterfaceStore } from './stores/UserInterfaceStore';

const App: FC = () => {
  const chatExpanded = UseUserInterfaceStore((store) => store.chatExpanded);
  const mapsExpanded = UseUserInterfaceStore((store) => store.mapsExpanded);

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <AppBar />
      <div className="flex flex-col-reverse lg:flex-row grow min-h-0">
        {chatExpanded && <ChatArea />}
        <SectionDivider />
        {mapsExpanded && <Maps />}
      </div>
    </div>
  );
};

export default App;
