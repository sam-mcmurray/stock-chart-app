// src/auth/AutoLoginGate.jsx
import { useEffect, useState } from 'react';
import { getAuthStatus, buildLoginUrl } from '../lib/api';
import { hardReplace } from '../lib/hardNav';
import { clearManualLogout, manualLogoutState } from './logoutState';

function currentPath() {
  return window.location.pathname + window.location.search + window.location.hash || '/';
}

export default function AutoLoginGate({ children }) {
  const [ready, setReady] = useState(false);
  const [signedOut, setSignedOut] = useState(null);

  useEffect(() => {
    let stop = false;

    (async () => {
      let res = null;
      try {
        res = await getAuthStatus();
      } catch {
        res = { ok: false };
      }
      if (stop) return;

      const isAuthed = !!res.ok && !!res.data?.authenticated;
      if (!isAuthed) {
        const logoutState = manualLogoutState();
        if (logoutState.active) {
          setSignedOut(logoutState);
          setReady(true);
          return;
        }
        const url = buildLoginUrl(currentPath());
        hardReplace(url);
        return;
      }

      setReady(true);
    })();

    return () => { stop = true; };
  }, []);

  if (ready && signedOut) {
    const signIn = () => {
      const url = buildLoginUrl('/dashboard');
      clearManualLogout();
      hardReplace(url);
    };
    return (
      <main className="auth-fallback">
        <div className="auth-fallback-content signed-out-content">
          <h1>Signed out</h1>
          <p>
            {signedOut.autoTraderRetained
              ? 'Your browser session ended. Auto Trader and its scanner market data remain active with isolated credentials.'
              : 'Your Schwab session and background market-data access have ended.'}
          </p>
          <button type="button" className="signed-out-login" onClick={signIn}>Sign in with Schwab</button>
        </div>
      </main>
    );
  }

  if (!ready) {
    // Brief fallback while we check / or while navigating
    const next =
      typeof window !== 'undefined'
        ? currentPath()
        : '/';
    const url = buildLoginUrl(next || '/');

    return (
      <main className="auth-fallback">
        <div className="auth-fallback-content">
          <div className="auth-fallback-status">Checking session…</div>
          <a href={url} className="auth-fallback-link">
            If nothing happens, click here to sign in
          </a>
        </div>
      </main>
    );
  }

  return children;
}
