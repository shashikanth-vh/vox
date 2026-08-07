import { Component, type ReactNode } from 'react';

// The last line of defence: a runtime crash anywhere in the tree must NEVER be a blank
// white page (undiagnosable from a phone). It renders the error message and a reload
// button instead — plain DOM only, so the boundary cannot itself depend on anything
// that may have just crashed (theme, router, stores).
export default class ErrorBoundary extends Component<
  { children: ReactNode }, { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    // eslint-disable-next-line no-console
    console.error('[ATLAS] render crash:', error, info?.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center',
        justifyContent: 'center', padding: 24, background: '#F6F8F8',
        fontFamily: 'system-ui, sans-serif' }}>
        <div style={{ maxWidth: 460, background: '#fff', border: '1px solid #E3E8E8',
          borderRadius: 12, padding: '22px 24px' }}>
          <div style={{ fontSize: 17, fontWeight: 700, marginBottom: 6 }}>
            Something went wrong on this screen
          </div>
          <div style={{ fontSize: 13, color: '#5B6B6B', marginBottom: 14 }}>
            Reloading usually fixes it — the app also refreshes itself to the latest
            version on reload. If it keeps happening, share this message with support:
          </div>
          <pre style={{ fontSize: 11.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            background: '#F3F5F5', borderRadius: 8, padding: '10px 12px',
            color: '#8A3B3B', margin: '0 0 16px' }}>
            {String(error?.message || error)}
          </pre>
          <button onClick={() => window.location.reload()}
            style={{ background: '#0B7C6B', color: '#fff', border: 'none',
              borderRadius: 8, padding: '10px 18px', fontSize: 14, fontWeight: 600,
              cursor: 'pointer' }}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
