import { afterEach, expect, it, vi } from 'vitest';
import { api, apiFetch, updateTradingStatus } from '../lib/api';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  document.cookie = 'csrf=; Max-Age=0; path=/';
  document.cookie = 'stockapp_starter_csrf=; Max-Age=0; path=/';
});

it('uses the separate starter cookie for each request helper', async () => {
  vi.stubEnv('VITE_CSRF_COOKIE_NAME', 'stockapp_starter_csrf');
  document.cookie = 'csrf=original-session-token; path=/';
  document.cookie = 'stockapp_starter_csrf=starter-session-token; path=/';
  const fetch = vi.fn(async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', fetch);
  await api('/auth/status');
  await apiFetch('/auth/status');
  await updateTradingStatus(false);
  for (const [, options] of fetch.mock.calls) {
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('starter-session-token');
  }
  expect(document.cookie).toContain('csrf=original-session-token');
});

it('retains the default cookie behavior when no starter override is supplied', async () => {
  vi.stubEnv('VITE_CSRF_COOKIE_NAME', '');
  document.cookie = 'csrf=default-session-token; path=/';
  const fetch = vi.fn(async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', fetch);
  await api('/auth/status');
  expect(new Headers(fetch.mock.calls[0][1].headers).get('X-CSRF-Token')).toBe('default-session-token');
});
