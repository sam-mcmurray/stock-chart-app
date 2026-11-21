import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Research from '../pages/Research';
import * as api from '../lib/api';

vi.mock('../lib/api', () => ({
  getLiveLearningStatus: vi.fn(), getTrainingDataStatus: vi.fn(),
  getHistoricalOutcomeReport: vi.fn(), getHistoricalPriceStatus: vi.fn(),
  getShadowEntrySummary: vi.fn(), getResearchExperiments: vi.fn(),
}));

describe('RL research priorities', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getLiveLearningStatus.mockResolvedValue({learner_status:'not_deployed',capture_status:'connected',symbol_count:25});
    api.getTrainingDataStatus.mockResolvedValue({status:'running',export:{days:30},acquisition:{},assembly:{}});
  });
  it('loads only the live model status by default, not legacy experiments', async () => {
    render(<Research />);
    expect(await screen.findByText('Not deployed')).toBeInTheDocument();
    expect(screen.getByText('connected')).toBeInTheDocument();
    expect(api.getTrainingDataStatus).not.toHaveBeenCalled();
    expect(api.getHistoricalOutcomeReport).not.toHaveBeenCalled();
    expect(api.getHistoricalPriceStatus).not.toHaveBeenCalled();
    expect(api.getShadowEntrySummary).not.toHaveBeenCalled();
    expect(api.getResearchExperiments).not.toHaveBeenCalled();
  });
  it('loads minute-bar preparation only when selected', async () => {
    render(<Research />);
    fireEvent.click(screen.getByRole('tab', {name:'1-minute model'}));
    expect(await screen.findByText('30')).toBeInTheDocument();
    expect(api.getTrainingDataStatus).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Learning pipeline')).not.toBeInTheDocument();
  });
  it('shows a deployed closed-market learner without claiming updates', async () => {
    api.getLiveLearningStatus.mockResolvedValue({learner_status:'market_closed', learner:{
      policy_version:0,process_training_transitions:0,active_symbols:[],heartbeat_age_seconds:2,
      account_scope:'independent_symbol_simulations'}});
    render(<Research />);
    expect(await screen.findByText('Market closed')).toBeInTheDocument();
    expect(screen.getByText('Completed PPO updates')).toBeInTheDocument();
    expect(screen.getByText('Independent symbol simulations')).toBeInTheDocument();
    expect(screen.queryByText('Pending')).not.toBeInTheDocument();
  });
});
