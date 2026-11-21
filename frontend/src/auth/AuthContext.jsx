import { useCallback, useEffect, useMemo, useState } from 'react';
import { buildLoginUrl, getAuthStatus, logout as apiLogout } from '../lib/api';
import { hardNavigate } from '../lib/hardNav';
import { AuthContext } from './authState';
import { clearManualLogout, markManualLogout } from './logoutState';

export function AuthProvider({ children }) {
  const [authenticated, setAuthenticated] = useState(null);
  const [authStatus, setAuthStatus] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const res = await getAuthStatus();
      if (res.ok) {
        setAuthenticated(!!res.data?.authenticated);
        setAuthStatus(res.data || null);
      }
      else { console.warn('auth/status failed:', res.error); setAuthenticated(false); }
    } catch (err) {
      console.warn('auth/status failed:', err);
      setAuthenticated(false);
    }
  }, []);

  function login(next = '/dashboard') {
    clearManualLogout();
    const url = buildLoginUrl(next);
    hardNavigate(url);
  }

  async function logout() {
    const res = await apiLogout();
    if (!res.ok) {
      console.warn('logout failed:', res.error);
      return res;
    }
    markManualLogout(Boolean(res.data?.trading_credentials_retained));
    setAuthenticated(false);
    setAuthStatus(null);
    return res;
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 15 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const value = useMemo(
    () => ({ authenticated, authStatus, loading: authenticated === null, refresh, login, logout }),
    [authenticated, authStatus, refresh],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
