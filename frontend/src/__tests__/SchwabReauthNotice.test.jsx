import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AuthContext } from '../auth/authState';
import SchwabReauthNotice from '../components/SchwabReauthNotice';

function renderNotice(authStatus, login = vi.fn()) {
  render(
    <MemoryRouter initialEntries={['/calendar?month=2026-07']}>
      <AuthContext.Provider value={{ authStatus, login }}>
        <SchwabReauthNotice />
      </AuthContext.Provider>
    </MemoryRouter>,
  );
  return login;
}

describe('SchwabReauthNotice', () => {
  it('stays hidden before the warning window', () => {
    renderNotice({ reauthorization_soon: false, seconds_to_reauthorization: 90000 });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows remaining time and reauthorizes back to the current page', () => {
    const login = renderNotice({ reauthorization_soon: true, seconds_to_reauthorization: 3700 });
    expect(screen.getByRole('alert')).toHaveTextContent('Authorization expires in 2 hours.');

    fireEvent.click(screen.getByRole('button', { name: /reauthorize/i }));

    expect(login).toHaveBeenCalledWith('/calendar?month=2026-07');
  });
});
