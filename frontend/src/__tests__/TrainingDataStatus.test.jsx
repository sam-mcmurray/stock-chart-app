import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import TrainingDataStatus from '../components/TrainingDataStatus';
import { getTrainingDataStatus } from '../lib/api';

vi.mock('../lib/api', () => ({ getTrainingDataStatus: vi.fn() }));

describe('Training data', () => {
  it('separates exports, requests, and unstarted assembly', async () => {
    getTrainingDataStatus.mockResolvedValue({status:'running',stage:'price_acquisition',current_month:'2026-06',
      completed_months:[], export:{days:30,articles:2286,symbols:744,first_date:'2026-06-01',last_date:'2026-06-30'},
      acquisition:{completed:305,remaining:1183,no_data:2},assembly:{}});
    render(<TrainingDataStatus />);
    expect(await screen.findByText('2,286')).toBeInTheDocument();
    expect(screen.getByText('305')).toBeInTheDocument();
    expect(screen.getByText('Assembled symbol-days').nextSibling).toHaveTextContent('Not available');
    expect(screen.getByText('Not started by this job')).toBeInTheDocument();
  });
  it('shows fetch failure without invented progress', async () => {
    getTrainingDataStatus.mockRejectedValue(new Error('Offline'));
    render(<TrainingDataStatus />);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Offline'));
    expect(screen.queryByText('0')).not.toBeInTheDocument();
  });
});
