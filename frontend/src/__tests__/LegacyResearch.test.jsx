import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Research from '../pages/LegacyResearch';
import * as api from '../lib/api';

vi.mock('../lib/api', () => ({
  getHistoricalOutcomeReport: vi.fn(),
  getHistoricalOutcomeStatus: vi.fn(),
  getHistoricalPriceStatus: vi.fn(),
  getResearchExperiments: vi.fn(),
  getLiveResearchStatus: vi.fn(),
  getLiveResearchReport: vi.fn(),
  getShadowEntryCandidates: vi.fn(),
  getShadowEntrySummary: vi.fn(),
}));

const metrics = {
  count: 100,
  mean_return_pct: -0.2,
  win_rate_pct: 40,
  mean_excess_return_pct: 0.1,
  mean_excess_return_ci_95_pct: [-0.1, 0.3],
};

describe('Research', () => {
  beforeEach(() => {
    api.getShadowEntrySummary.mockResolvedValue({
      candidates: { monitoring: 1, entered: 2, rejected: 3 },
      opportunities: { watching: 2, monitoring: 1, entered: 1, expired: 3 },
      positions: { open: 1 }, candidate_checks: 12, opportunity_checks: 7,
    });
    api.getShadowEntryCandidates.mockResolvedValue([{
      candidate_id: 'candidate-1', symbol: 'ABCD', status: 'monitoring',
      reason: 'entry_quote_invalid_or_stale', check_count: 7,
      signal_ts: 1789000000, evaluated_at: '2026-09-10T12:01:00Z',
      expires_ts: 1789000900,
    }]);
  });

  it('shows shadow results even while historical requests never finish', async () => {
    api.getHistoricalOutcomeReport.mockReturnValue(new Promise(() => {}));
    api.getHistoricalPriceStatus.mockReturnValue(new Promise(() => {}));
    api.getHistoricalOutcomeStatus.mockResolvedValue({});
    api.getResearchExperiments.mockResolvedValue([]);
    api.getLiveResearchStatus.mockResolvedValue({});
    api.getLiveResearchReport.mockResolvedValue({});
    render(<Research />);
    fireEvent.click(screen.getByRole('tab', { name: 'entry reevaluation' }));
    expect(await screen.findByText('ABCD')).toBeInTheDocument();
    expect(screen.getByText('Shadow Entry Reevaluation')).toBeInTheDocument();
  });

  it('polls a queued snapshot without displaying false zero results', async () => {
    vi.useFakeTimers();
    try {
      api.getHistoricalOutcomeReport.mockResolvedValueOnce({ status: 'building' })
        .mockResolvedValue({ usable_outcome_count: 42, overall: {}, snapshot: { generated_ts: 1790000000 } });
      api.getHistoricalPriceStatus.mockResolvedValue({ coverage: {} });
      api.getHistoricalOutcomeStatus.mockResolvedValue({});
      api.getResearchExperiments.mockResolvedValue([]);
      api.getLiveResearchStatus.mockResolvedValue({});
      api.getLiveResearchReport.mockResolvedValue({});
      await act(async () => { render(<Research />); });
      expect(screen.getByText('Historical snapshot building...')).toBeInTheDocument();
      expect(screen.queryByText('Usable outcomes')).not.toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.getByText('42')).toBeInTheDocument();
      expect(screen.queryByText('Historical snapshot building...')).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it('keeps successful sections when a historical request fails', async () => {
    api.getHistoricalOutcomeReport.mockRejectedValue(new Error('Historical report is busy'));
    api.getHistoricalPriceStatus.mockResolvedValue({ coverage: {} });
    api.getResearchExperiments.mockResolvedValue([]);
    render(<Research />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Historical report is busy');
    fireEvent.click(screen.getByRole('tab', { name: 'entry reevaluation' }));
    expect(await screen.findByText('ABCD')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh research results' })).toBeEnabled());
  });

  it('shows retrospective results in separate compact views', async () => {
    api.getHistoricalOutcomeReport.mockResolvedValue({
      usable_outcome_count: 885,
      benchmark_symbol: 'SPY',
      feature_version: 'news-hype-v1',
      generator_version: 'news-hype-candidate-generator-v1',
      available_versions: [{
        feature_version: 'news-hype-v1', generator_version: 'news-hype-candidate-generator-v1',
        candidate_count: 885, outcome_count: 885,
      }],
      descriptive_bucket_method: 'equal-count score ranks',
      selection_policy: 'training-period threshold',
      overall: { ...metrics, mean_benchmark_return_pct: -0.1 },
      descriptive_rank_buckets: [{
        bucket: 'top_quartile', minimum_hype_score: 0.1, maximum_hype_score: 0.8, ...metrics,
      }],
      walk_forward: [{
        fold: 1, test_start_ts: 1789000000, test_end_ts: 1789100000,
        training_top_quartile_threshold: 0.1, test_selected: metrics,
      }],
      by_session: [{ session: 'regular', ...metrics }],
      by_symbol: [{ symbol: 'NVDA', ...metrics }],
      by_catalyst: [],
      by_source_record_type: [],
      hypothesis_cohort: {
        definition: 'long_hype_score equals zero',
        descriptive_rank_buckets: [{
          bucket: 'top_quartile', minimum_hype_score: 0.2, maximum_hype_score: 0.8,
          count: 10, mean_mfe_pct: 1.2, mean_mfe_ci_95_pct: [0.8, 1.6],
          mean_return_pct: 0.1, mean_excess_return_pct: 0.2,
        }],
      },
    });
    api.getHistoricalOutcomeStatus.mockResolvedValue({
      outcomes: { complete: 490, partial: 437, insufficient: 87 }, ready_pending: 0,
    });
    api.getHistoricalPriceStatus.mockResolvedValue({ coverage: { count: 466500 } });
    api.getResearchExperiments.mockResolvedValue([]);
    api.getLiveResearchStatus.mockResolvedValue({
      hypothesis_cohort_count: 0, hypothesis_cohort_outcome_count: 0, sample_progress_pct: 0,
    });
    api.getLiveResearchReport.mockResolvedValue({
      formal_readout_available: false,
      hypothesis_cohort: { count: 0 },
      top_attention_quartile: { count: 0 },
    });

    render(<Research />);

    expect(await screen.findByText('Outcome Research')).toBeInTheDocument();
    expect(screen.getByText('885')).toBeInTheDocument();
    expect(screen.getByText('466,500')).toBeInTheDocument();
    expect(screen.getAllByText('top quartile')).toHaveLength(2);
    expect(screen.getByText('Zero-Direction Watch Cohort')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'walk forward' }));
    expect(screen.getByText('Walk-Forward Tests')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'breakdowns' }));
    expect(screen.getByText('regular')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Symbol' }));
    expect(screen.getByText('NVDA')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'entry reevaluation' }));
    expect(screen.getByText('Shadow Entry Reevaluation')).toBeInTheDocument();
    expect(screen.getByText('ABCD')).toBeInTheDocument();
    expect(screen.getByText('entry quote invalid or stale')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Horizon'), { target: { value: '15' } });
    await waitFor(() => expect(api.getHistoricalOutcomeReport).toHaveBeenLastCalledWith(
      15, 4, 'news-hype-v1', 'news-hype-candidate-generator-v1',
    ));
  });

  it('shows the immutable boundary for a selected preregistered experiment', async () => {
    api.getHistoricalOutcomeReport.mockResolvedValue({
      usable_outcome_count: 0,
      feature_version: 'news-hype-v2-watch-priority',
      generator_version: 'news-hype-candidate-generator-v2-watch-priority',
      ranking_score_field: 'candidate_priority_score',
      available_versions: [{
        feature_version: 'news-hype-v2-watch-priority',
        generator_version: 'news-hype-candidate-generator-v2-watch-priority',
      }],
      overall: {}, descriptive_rank_buckets: [], walk_forward: [],
      by_session: [], by_symbol: [], by_catalyst: [], by_source_record_type: [],
    });
    api.getHistoricalOutcomeStatus.mockResolvedValue({ outcomes: {} });
    api.getHistoricalPriceStatus.mockResolvedValue({ coverage: {} });
    api.getResearchExperiments.mockResolvedValue([{
      experiment_version: 'news-hype-watch-priority-v2',
      feature_version: 'news-hype-v2-watch-priority',
      generator_version: 'news-hype-candidate-generator-v2-watch-priority',
      hypothesis: 'Attention ranks watch candidates without becoming an entry signal.',
      registered_at: 1789756100,
      primary_metric: 'mean_mfe_pct', horizon_bars: 60,
      minimum_post_registration_live_received_sample: 500,
    }]);
    api.getLiveResearchStatus.mockResolvedValue({
      hypothesis_cohort_count: 12, sample_progress_pct: 2.4,
      hypothesis_cohort_outcome_count: 12,
      partial_outcome_count: 8, unevaluated_count: 3,
      insufficient_outcome_count: 2,
      collection_scheduler: {
        version: 'live-stream-priority-v2', started_at: 1790122900,
        candidate_count: 9, collecting_count: 4, complete_count: 3,
        insufficient_count: 2, late_candle_count: 1, no_candle_count: 1,
      },
    });
    api.getLiveResearchReport.mockResolvedValue({
      formal_readout_available: false,
      benchmark_symbol: 'SPY',
      hypothesis_cohort: {
        count: 12, mean_mfe_pct: 0.8,
        benchmark_coverage_count: 10, mean_excess_return_pct: 0.45,
      },
      top_attention_quartile: { count: 3, mean_mfe_pct: 1.1 },
      top_vs_cohort_mean_mfe_difference_pct: 0.3,
    });

    render(<Research />);

    expect(await screen.findByText('Preregistered experiment')).toBeInTheDocument();
    expect(screen.getByText('Primary: mean_mfe_pct')).toBeInTheDocument();
    expect(screen.getByText('500 live-received minimum')).toBeInTheDocument();
    expect(screen.getByText('12 independent live episodes')).toBeInTheDocument();
    expect(screen.getByText('12 complete cohort outcomes')).toBeInTheDocument();
    expect(screen.getByText('8 collecting')).toBeInTheDocument();
    expect(screen.getByText('3 awaiting evaluation')).toBeInTheDocument();
    expect(screen.getByText('2 missed entry window')).toBeInTheDocument();
    expect(screen.getByText('2.4% sample progress')).toBeInTheDocument();
    expect(screen.getByText(/live-stream-priority-v2 since/)).toBeInTheDocument();
    expect(screen.getByText('9 post-fix candidates')).toBeInTheDocument();
    expect(screen.getByText('4 post-fix collecting')).toBeInTheDocument();
    expect(screen.getByText('3 post-fix complete')).toBeInTheDocument();
    expect(screen.getByText('2 post-fix missed')).toBeInTheDocument();
    expect(screen.getByText('Accumulating')).toBeInTheDocument();
    expect(screen.getByText('0.800%')).toBeInTheDocument();
    expect(screen.getByText('Cohort excess vs SPY')).toBeInTheDocument();
    expect(screen.getByText('0.450%')).toBeInTheDocument();
    expect(screen.getByText('10 / 12')).toBeInTheDocument();
    expect(screen.getByText('1.100%')).toBeInTheDocument();
    expect(screen.getByText('0.300%')).toBeInTheDocument();
  });
});
