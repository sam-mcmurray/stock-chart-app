import { createContext, useContext } from 'react';

export const AuthContext = createContext({
  authenticated: null,
  authStatus: null,
  loading: true,
  refresh: async () => {},
  login: () => {},
  logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);
