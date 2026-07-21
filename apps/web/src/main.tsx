import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import Embed from './Embed';
import './index.css';

const queryClient = new QueryClient();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {new URLSearchParams(window.location.search).has('embed') ? <Embed /> : <App />}
    </QueryClientProvider>
  </StrictMode>,
);
