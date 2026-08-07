import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ThemeProvider, CssBaseline } from '@mui/material';
import { QueryClientProvider } from '@tanstack/react-query';
import theme from './theme';
import { queryClient } from './utils/queryClient';
import { AuthProvider } from './auth/AuthContext';
import { SearchProvider } from './context/SearchContext';
import ErrorBoundary from './components/common/ErrorBoundary';
import App from './App';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* Outermost, above every provider: a crash anywhere below renders a readable
        message + reload button, never a blank page (mobile has no console). */}
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider theme={theme}>
          <CssBaseline />
          <AuthProvider>
            <SearchProvider>
              {/* basename follows Vite's base, so the SAME bundle works at / (dev)
                  and under /ui/ behind the PRISM edge (vite build --base=/ui/). */}
              <BrowserRouter basename={import.meta.env.BASE_URL}>
                <App />
              </BrowserRouter>
            </SearchProvider>
          </AuthProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
