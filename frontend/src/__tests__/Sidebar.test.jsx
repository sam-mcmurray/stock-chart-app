import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import Sidebar from '../components/Sidebar';
import { AuthContext } from '../auth/authState';
import * as hardNav from '../lib/hardNav';

vi.mock('../lib/hardNav', () => ({ hardReplace: vi.fn() }));

function renderSidebar(auth, props = {}) {
  return render(
    <MemoryRouter>
      <AuthContext.Provider value={auth}>
        <Sidebar
          collapsed={false}
          onToggle={vi.fn()}
          isMobile={false}
          drawerOpen={false}
          onClose={vi.fn()}
          {...props}
        />
      </AuthContext.Provider>
    </MemoryRouter>
  );
}

describe('Sidebar logout', () => {
  it('logs out and opens the local signed-out state after success', async () => {
    const logout = vi.fn().mockResolvedValue({ ok: true });
    const login = vi.fn();
    renderSidebar({ logout, login });

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    await waitFor(() => expect(logout).toHaveBeenCalledOnce());
    expect(login).not.toHaveBeenCalled();
    expect(hardNav.hardReplace).toHaveBeenCalledWith('/');
  });

  it('shows an error and does not redirect when server logout fails', async () => {
    const logout = vi.fn().mockResolvedValue({ ok: false, error: 'Network error' });
    const login = vi.fn();
    renderSidebar({ logout, login });

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Network error');
    expect(login).not.toHaveBeenCalled();
  });

  it('closes the mobile drawer after successful logout', async () => {
    const logout = vi.fn().mockResolvedValue({ ok: true });
    const login = vi.fn();
    const onClose = vi.fn();
    renderSidebar(
      { logout, login },
      { isMobile: true, drawerOpen: true, onClose }
    );

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(login).not.toHaveBeenCalled();
    expect(hardNav.hardReplace).toHaveBeenCalledWith('/');
  });
});
