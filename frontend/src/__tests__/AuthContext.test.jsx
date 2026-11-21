// frontend/src/__tests__/AuthContext.test.jsx
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { AuthProvider } from '../auth/AuthContext';
import { useAuth } from '../auth/authState';
import * as api from '../lib/api';
import * as hardNav from '../lib/hardNav';

// Mock the modules this context depends on
vi.mock('../lib/api', () => ({
  getAuthStatus: vi.fn(),
  logout: vi.fn(),
  buildLoginUrl: vi.fn((next) => `http://mock.login.url?next=${next}`),
}));
vi.mock('../lib/hardNav', () => ({
  hardNavigate: vi.fn(),
}));

// A wrapper component to use the hook
const wrapper = ({ children }) => <AuthProvider>{children}</AuthProvider>;

describe('AuthProvider', () => {

  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  it('initial state should be loading and not authenticated', async () => {
    // Mock the initial status check to be pending
    api.getAuthStatus.mockReturnValue(new Promise(() => {}));
    
    const { result } = renderHook(() => useAuth(), { wrapper });

    expect(result.current.loading).toBe(true);
    expect(result.current.authenticated).toBe(null);
  });

  it('should be authenticated if getAuthStatus returns authenticated: true', async () => {
    api.getAuthStatus.mockResolvedValue({ ok: true, data: { authenticated: true } });
    
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await Promise.resolve(); // Let the useEffect in the provider run
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.authenticated).toBe(true);
  });

  it('should not be authenticated if getAuthStatus returns authenticated: false', async () => {
    api.getAuthStatus.mockResolvedValue({ ok: true, data: { authenticated: false } });
    
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.authenticated).toBe(false);
  });

  it('should call hardNavigate with the correct URL on login', () => {
    api.getAuthStatus.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useAuth(), { wrapper });

    act(() => {
      result.current.login('/profile');
    });

    expect(api.buildLoginUrl).toHaveBeenCalledWith('/profile');
    expect(hardNav.hardNavigate).toHaveBeenCalledWith('http://mock.login.url?next=/profile');
  });

  it('should log out, call apiLogout, and set authenticated to false', async () => {
    // Start in an authenticated state
    api.getAuthStatus.mockResolvedValue({ ok: true, data: { authenticated: true } });
    api.logout.mockResolvedValue({ ok: true });
    
    const { result } = renderHook(() => useAuth(), { wrapper });

    // Wait for initial authentication
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.authenticated).toBe(true);

    // Trigger logout
    await act(async () => {
      await result.current.logout();
    });

    expect(api.logout).toHaveBeenCalled();
    expect(result.current.authenticated).toBe(false);
  });
  
  it('should become unauthenticated if refresh fails', async () => {
    api.getAuthStatus.mockRejectedValue(new Error("API Down"));
    
    const { result } = renderHook(() => useAuth(), { wrapper });
    
    await act(async () => {
      await Promise.resolve(); // let useEffect run
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.authenticated).toBe(false);
    expect(console.warn).toHaveBeenCalled();
  });
});
