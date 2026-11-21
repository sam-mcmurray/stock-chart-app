import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Settings from '../pages/Settings';
import * as api from '../lib/api';

vi.mock('../lib/api', () => ({
  getScannerSettings: vi.fn(),
  updateScannerSettings: vi.fn(),
}));

describe('Settings', () => {
  const settings = {
    price_min: 0,
    price_max: 20,
    rvol_threshold: 3,
    rvol_window_days: 60,
    float_max: 20000000,
    scan_interval: 30,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    api.getScannerSettings.mockResolvedValue(settings);
  });

  it('loads scanner settings', async () => {
    render(<Settings />);

    expect(screen.getByText('Loading settings...')).toBeInTheDocument();
    expect(await screen.findByLabelText('Minimum price')).toHaveValue(0);
    expect(screen.getByLabelText('Maximum price')).toHaveValue(20);
  });

  it('saves scanner settings', async () => {
    api.updateScannerSettings.mockResolvedValue({...settings, price_max: 15});
    render(<Settings />);

    const maxPrice = await screen.findByLabelText('Maximum price');
    fireEvent.change(maxPrice, {target: {value: '15'}});
    fireEvent.click(screen.getByRole('button', {name: 'Save'}));

    await waitFor(() => {
      expect(api.updateScannerSettings).toHaveBeenCalledWith({
        ...settings,
        price_max: 15,
      });
    });
    expect(await screen.findByRole('status')).toHaveTextContent('Settings saved.');
  });

  it('validates scanner settings before saving', async () => {
    render(<Settings />);

    const minPrice = await screen.findByLabelText('Minimum price');
    const maxPrice = screen.getByLabelText('Maximum price');
    fireEvent.change(minPrice, {target: {value: '25'}});
    fireEvent.change(maxPrice, {target: {value: '15'}});
    fireEvent.click(screen.getByRole('button', {name: 'Save'}));

    expect(await screen.findByRole('alert')).toHaveTextContent('Min price must be less than or equal to max price.');
    expect(api.updateScannerSettings).not.toHaveBeenCalled();
  });

  it('shows settings load failures', async () => {
    api.getScannerSettings.mockRejectedValue(new Error('settings unavailable'));

    render(<Settings />);

    expect(await screen.findByText('settings unavailable')).toBeInTheDocument();
  });
});
