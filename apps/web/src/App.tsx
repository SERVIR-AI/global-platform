import type { FC } from 'react';
import AppBar from './components/AppBar';
import ChatArea from './components/ChatArea';
import Maps from './components/Maps';

const App: FC = () => {
  return (
    <div className="min-h-screen flex flex-col">
      <AppBar />
      <div className="flex flex-col-reverse lg:flex-row grow">
        <ChatArea />
        <Maps />
      </div>
    </div>
  );
};

export default App;
