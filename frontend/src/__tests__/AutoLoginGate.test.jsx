import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AutoLoginGate from '../auth/AutoLoginGate';
import * as api from '../lib/api';
import * as hardNav from '../lib/hardNav';

vi.mock('../lib/api', () => ({
  buildLoginUrl: vi.fn((next) => `https://127.0.0.1:8443/auth/login?next=${encodeURIComponent(next)}`),
  getAuthStatus: vi.fn(),
}));

vi.mock('../lib/hardNav', () => ({
  hardReplace: vi.fn(),
}));

describe('AutoLoginGate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState({}, '', '/scanner?tab=active#row-1');
  });

  it('renders fallback link while auth status is pending', () => {
    api.getAuthStatus.mockReturnValue(new Promise(() => {}));

    render(
      <AutoLoginGate>
        <div>Private app</div>
      </AutoLoginGate>
    );

    expect(screen.getByText('Checking session…')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /click here to sign in/i })).toHaveAttribute(
      'href',
      'https://127.0.0.1:8443/auth/login?next=%2Fscanner%3Ftab%3Dactive%23row-1'
    );
  });

  it('renders children when authenticated', async () => {
    api.getAuthStatus.mockResolvedValue({ ok: true, data: { authenticated: true } });

    render(
      <AutoLoginGate>
        <div>Private app</div>
      </AutoLoginGate>
    );

    expect(await screen.findByText('Private app')).toBeInTheDocument();
    expect(hardNav.hardReplace).not.toHaveBeenCalled();
  });

  it('redirects to Schwab login when unauthenticated', async () => {
    api.getAuthStatus.mockResolvedValue({ ok: true, data: { authenticated: false } });

    render(
      <AutoLoginGate>
        <div>Private app</div>
      </AutoLoginGate>
    );

    await waitFor(() => {
      expect(hardNav.hardReplace).toHaveBeenCalledWith(
        'https://127.0.0.1:8443/auth/login?next=%2Fscanner%3Ftab%3Dactive%23row-1'
      );
    });
  });

  it('treats auth status failures as unauthenticated', async () => {
    api.getAuthStatus.mockRejectedValue(new Error('status failed'));

    render(
      <AutoLoginGate>
        <div>Private app</div>
      </AutoLoginGate>
    );

    await waitFor(() => {
      expect(hardNav.hardReplace).toHaveBeenCalled();
    });
  });

  it('shows an explicit signed-out state without automatically logging in', async () => {
    localStorage.setItem('stockapp_manual_logout', '1');
    localStorage.setItem('stockapp_logout_auto_trader_retained', '1');
    api.getAuthStatus.mockResolvedValue({ ok: true, data: { authenticated: false } });

    render(<AutoLoginGate><div>Private app</div></AutoLoginGate>);

    expect(await screen.findByRole('heading', { name: 'Signed out' })).toBeInTheDocument();
    expect(screen.getByText(/Auto Trader and its scanner market data remain active/)).toBeInTheDocument();
    expect(hardNav.hardReplace).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Sign in with Schwab' }));
    expect(hardNav.hardReplace).toHaveBeenCalledWith(
      'https://127.0.0.1:8443/auth/login?next=%2Fdashboard',
    );
  });
});
