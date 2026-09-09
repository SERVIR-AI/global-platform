import type { FC } from 'react';
import AppBar from './components/AppBar';
import ChatArea from './components/ChatArea';
import Maps from './components/Maps';
import ProvenancePanel from './components/Provenance';
import SectionDivider from './components/SectionDivider';
import SignedOut from './components/SignedOut';
import { useAuth } from './hooks/useAuth';
import { useChatStore } from './stores/ChatStore';
import { UseUserInterfaceStore } from './stores/UserInterfaceStore';

const App: FC = () => {
  const { status } = useAuth();
  const chatExpanded = UseUserInterfaceStore((store) => store.chatExpanded);
  const mapsExpanded = UseUserInterfaceStore((store) => store.mapsExpanded);
  const useCase = useChatStore((store) => store.useCase);

  // Signed-out visitors see the account chrome and the sign-in panel instead of
  // the (gated) app. During `loading` the shell keeps rendering so a signed-in
  // user never sees a sign-in flash before /auth/me resolves; auth-off (web
  // login not mounted) renders the app exactly as before auth existed.
  if (status === 'signed-out') {
    return (
      <div className="h-screen flex flex-col overflow-hidden">
        <AppBar />
        <SignedOut />
      </div>
    );
  }

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
