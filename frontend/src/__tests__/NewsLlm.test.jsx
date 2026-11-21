import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import NewsLlm from '../pages/NewsLlm';
import * as api from '../lib/api';
import { downloadTextFile } from '../lib/calendarFiles';

vi.mock('../lib/api', () => ({
  deleteNewsLlmModel: vi.fn(),
  downloadAcceptedNewsTrainingData: vi.fn(),
  evaluateFinancialSentiment: vi.fn(),
  deleteSentimentBenchmarkCase: vi.fn(),
  getNewsLlmStatus: vi.fn(),
  getNewsLlmQuality: vi.fn(),
  getNewsLlmWatch: vi.fn(),
  getSentimentModelStatus: vi.fn(),
  getSentimentEvaluations: vi.fn(),
  getSentimentBenchmarkCases: vi.fn(),
  importSentimentBenchmarkCases: vi.fn(),
  loadSentimentModel: vi.fn(),
  pullNewsLlmModel: vi.fn(),
  reviewNewsLlmSample: vi.fn(),
  retryNewsLlmFailures: vi.fn(),
  runSentimentBenchmark: vi.fn(),
  saveSentimentBenchmarkCase: vi.fn(),
  previewNewsLlmArticle: vi.fn(),
  previewFinancialSentiment: vi.fn(),
  searchNewsLlmCatalog: vi.fn(),
  searchSentimentModels: vi.fn(),
  unloadSentimentModel: vi.fn(),
  updateNewsLlmSettings: vi.fn(),
}));

vi.mock('../lib/calendarFiles', () => ({
  downloadBlob: vi.fn(),
  downloadTextFile: vi.fn(),
}));

describe('NewsLlm', () => {
  it('shows a shared newest-first pool without the old partition labels', async () => {
    const status = await api.getNewsLlmStatus();
    api.getNewsLlmStatus.mockResolvedValue({
      ...status,
      historical_operations: { ...status.historical_operations,
        queue_mode: 'shared_newest_first', remaining: 100, total: 1670,
        sustained_rate_per_minute: 10, measured_completions: 600,
        tracking_elapsed_seconds: 3600 },
    });
    render(<NewsLlm />);
    expect(await screen.findByText('Shared newest-first queue')).toBeInTheDocument();
    expect(screen.getByText('Remaining scan estimate')).toBeInTheDocument();
    expect(screen.getByText('Sustained completions')).toBeInTheDocument();
    expect(screen.getByText('Measured completions')).toBeInTheDocument();
    expect(screen.getByText('Observation window')).toBeInTheDocument();
    expect(screen.queryByText('Parallel backlog estimate')).not.toBeInTheDocument();
    expect(screen.getByRole('generic', { name: 'Historical worker 2' })).toBeInTheDocument();
    expect(screen.queryByText(/Recent and older workers meet/)).not.toBeInTheDocument();
    expect(screen.queryByText('Older cursor')).not.toBeInTheDocument();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    api.getNewsLlmStatus.mockResolvedValue({
      available: true,
      version: '1.0',
      host_port: 11435,
      gpu_device: '0',
      enabled: true,
      active_model: 'qwen3:1.7b',
      active_model_installed: true,
      live_worker: {
        live_pending: 12,
        backlog_pending: 108,
        failed: 115,
        records_per_minute: 12,
        eta_seconds: 600,
      },
      historical_backfill: {
        status: 'processing_historical_articles',
        calendar_completed: 8356,
        historical_completed: 1250,
        historical_remaining: 150000,
        historical_eta_seconds: 750000,
        historical_cursor_published_ts: 1777593600,
        batch_size: 8,
        records_per_minute: 11.2,
        resolve_seconds: 6.1,
        inference_seconds: 15.1,
      },
      historical_helper: {
        status: 'waiting_for_live_queue',
        historical_completed: 320,
        historical_remaining: 4300000,
        historical_eta_seconds: 24571429,
        historical_cursor_published_ts: 1541721600,
        batch_size: 4,
        records_per_minute: 10.5,
      },
      historical_operations: {
        completed: 1570,
        remaining: 4450000,
        total: 4451570,
        progress_percent: 0.04,
        rate_per_minute: 21.7,
        parallel_eta_seconds: 24571429,
        partition_boundary_ts: 1541721600,
        workers: {
          recent: { cursor_published_ts: 1777593600 },
          older: { cursor_published_ts: 1541721600 },
        },
      },
      models: [{
        name: 'qwen3:1.7b',
        size: 1500000000,
        details: { parameter_size: '1.7B', quantization_level: 'Q4_K_M' },
      }],
      running_models: [],
    });
    api.getNewsLlmWatch.mockResolvedValue({
      phase: 'completed',
      queue: 'live',
      batch_size: 2,
      updated_at: 1784851200,
      inputs: [{
        id: 'article-1',
        title: 'Luminar works with NVIDIA',
        summary: 'The companies announced a lidar partnership.',
        source: 'Example',
      }],
      outputs: {
        'article-1': {
          relevance_label: 'business',
          related_tickers: ['LAZR', 'NVDA'],
        },
      },
    });
    api.getNewsLlmQuality.mockResolvedValue({
      summary: {
        completed: 183031,
        retry_pending: 42,
        automated_acceptance: {
          accepted: 140000,
          provisional: 39000,
          needs_attention: 4031,
          accepted_percent: 76.5,
          source_grounded_percent: 82.1,
          confidence_passed_percent: 91.4,
        },
        coverage: {
          tickers_percent: 6.8,
          companies_percent: 6.2,
          relationships_percent: 1.3,
          canonical_verified_percent: 37.4,
        },
      },
      samples: [{
        category: 'ticker',
        url: 'https://example.com/nvidia',
        display_headline: 'NVIDIA announces a partnership',
        related_tickers: ['NVDA'],
        related_companies: [{ name: 'NVIDIA', validated_tickers: ['NVDA'], evidence: 'NVIDIA announced' }],
        llm_summary: 'NVIDIA announced a new partnership.',
        relevance_reason: 'Public company partnership news',
        llm_topics: ['partnership'],
      }, {
        category: 'unrelated',
        url: 'https://example.com/weather',
        display_headline: 'Local weather forecast',
        relevance_label: 'unrelated',
        relevance_confidence: 0.96,
      }],
      cache: {
        generated_at: 1784851200,
        fresh_for_seconds: 21600,
        refresh_in_progress: false,
      },
    });
    api.getSentimentModelStatus.mockResolvedValue({
      available: true,
      loaded: true,
      cuda: true,
      requested_device: 'cuda',
      device: 'cuda:0',
      gpu_name: 'NVIDIA GeForce RTX 5060 Ti',
      vram_allocated: 500000000,
      model: 'ProsusAI/finbert',
    });
    api.getSentimentEvaluations.mockResolvedValue({
      available: true,
      total: 1,
      case_total: 1,
      case_labels: { positive: 1 },
      recent_cases: [{
        case_key: 'case-1',
        headline: 'Company raises full-year revenue guidance',
        expected_label: 'positive',
        source_url: 'https://example.com/guidance',
      }],
      models: [{ model: 'ProsusAI/finbert', total: 1, correct: 1, accuracy_percent: 100 }],
      recent: [{
        evaluation_key: 'evaluation-1',
        headline: 'Earlier earnings result',
        expected_label: 'positive',
        predicted_label: 'positive',
        correct: true,
      }],
    });
    api.searchSentimentModels.mockResolvedValue({
      models: [{
        id: 'ProsusAI/finbert',
        downloads: 4800000,
        likes: 1200,
        has_safetensors: false,
        load_allowed: true,
        training: 'Financial PhraseBank financial news sentences',
        limitation: 'Measures tone; it does not predict price direction.',
        url: 'https://huggingface.co/ProsusAI/finbert',
      }],
    });
    api.loadSentimentModel.mockResolvedValue({ available: true, loaded: true, model: 'ProsusAI/finbert', cuda: true, device: 'cuda:0' });
    api.unloadSentimentModel.mockResolvedValue({ available: true, loaded: false, model: null, cuda: true, device: 'cuda:0' });
    api.previewFinancialSentiment.mockResolvedValue({ label: 'positive', confidence: 0.91, score: 0.88 });
    api.evaluateFinancialSentiment.mockResolvedValue({
      evaluation_key: 'evaluation-2',
      model: 'ProsusAI/finbert',
      expected_label: 'positive',
      predicted_label: 'positive',
      label: 'positive',
      confidence: 0.91,
      score: 0.88,
      correct: true,
    });
    api.saveSentimentBenchmarkCase.mockResolvedValue({ case_key: 'case-2', expected_label: 'neutral' });
    api.getSentimentBenchmarkCases.mockResolvedValue({
      cases: [{
        case_key: 'case-1',
        headline: 'Company raises full-year revenue guidance',
        summary: 'Demand improved.',
        expected_label: 'positive',
        source_url: 'https://example.com/guidance',
        note: 'Reviewed manually',
        predicted_label: 'negative',
      }],
    });
    api.importSentimentBenchmarkCases.mockResolvedValue({ imported: 1 });
    api.deleteSentimentBenchmarkCase.mockResolvedValue({ deleted: true, case_key: 'case-1' });
    api.runSentimentBenchmark.mockResolvedValue({
      evaluated: 1,
      summary: {
        available: true,
        total: 2,
        case_total: 1,
        case_labels: { positive: 1 },
        models: [{ model: 'ProsusAI/finbert', total: 2, correct: 2, accuracy_percent: 100 }],
        recent: [],
      },
    });
    api.searchNewsLlmCatalog.mockResolvedValue({
      models: [{
        name: 'qwen3:4b',
        description: 'Compact model',
        capabilities: ['tools'],
        parameter_sizes: ['4b'],
        pulls: '1M',
        tag_count: 4,
        updated_at: 'Jul 1, 2026 1:00 PM UTC',
        url: 'https://ollama.com/library/qwen3',
        variants: [{
          name: 'qwen3:4b',
          size: '2.5GB',
          context_window: '40K',
          input: 'Text',
          updated: '1 month ago',
        }],
      }],
    });
    api.pullNewsLlmModel.mockResolvedValue({ status: 'success' });
    api.previewNewsLlmArticle.mockResolvedValue({
      display_headline: 'Company reports quarterly results',
      relevance_label: 'business',
      relevance_confidence: 0.9,
      related_companies: [{ name: 'Company' }],
      related_tickers: [],
      llm_summary: 'A company reported quarterly results.',
      relevance_reason: 'Company earnings',
    });
    api.updateNewsLlmSettings.mockResolvedValue({ enabled: true, active_model: 'qwen3:1.7b' });
    api.reviewNewsLlmSample.mockResolvedValue({ judgment: 'correct' });
    api.retryNewsLlmFailures.mockResolvedValue({ replayed: 115, failed_remaining: 0 });
  });

  it('shows isolated GPU, active model, and searches the Ollama catalog', async () => {
    render(<NewsLlm />);

    expect((await screen.findAllByText('qwen3:1.7b')).length).toBeGreaterThan(0);
    expect(screen.getByText('GPU 0 only')).toBeInTheDocument();
    expect(screen.getByText('processing historical articles')).toBeInTheDocument();
    expect(screen.getByText('1,250')).toBeInTheDocument();
    expect(screen.getByText('8,356')).toBeInTheDocument();
    expect(screen.getByText('waiting for live queue')).toBeInTheDocument();
    expect(screen.getByText('320')).toBeInTheDocument();
    const ollamaSearch = screen.getByPlaceholderText('Search Ollama models').closest('.news-llm-search');
    fireEvent.click(within(ollamaSearch).getByRole('button', { name: /^search$/i }));

    expect((await screen.findAllByText('qwen3:4b')).length).toBeGreaterThan(0);
    expect(screen.getByText('2.5GB')).toBeInTheDocument();
    expect(screen.getByText('Jul 1, 2026 1:00 PM UTC')).toBeInTheDocument();
    expect(api.searchNewsLlmCatalog).toHaveBeenCalledWith('');
  });

  it('pulls an exact model name', async () => {
    render(<NewsLlm />);
    await screen.findByText('GPU 0 only');
    fireEvent.change(screen.getByPlaceholderText(/Exact model and variant/i), { target: { value: 'qwen3:4b' } });
    fireEvent.click(screen.getByRole('button', { name: /pull model/i }));

    await waitFor(() => expect(api.pullNewsLlmModel).toHaveBeenCalledWith('qwen3:4b'));
  });

  it('shows the Ollama pull response as an error', async () => {
    api.pullNewsLlmModel.mockRejectedValueOnce(new Error('Ollama request failed (500): unsupported model'));
    render(<NewsLlm />);
    await screen.findByText('GPU 0 only');
    fireEvent.change(screen.getByPlaceholderText(/Exact model and variant/i), { target: { value: 'qwen3.5:4b' } });
    fireEvent.click(screen.getByRole('button', { name: /pull model/i }));

    const message = await screen.findByText(/unsupported model/i);
    expect(message).toHaveClass('state-error');
  });

  it('previews the active model without enabling the worker', async () => {
    render(<NewsLlm />);
    await screen.findByText('GPU 0 only');
    fireEvent.click(screen.getByRole('button', { name: /run preview/i }));

    expect(await screen.findByText('Company reports quarterly results')).toBeInTheDocument();
    expect(api.previewNewsLlmArticle).toHaveBeenCalledWith(expect.objectContaining({
      title: 'company_report_quarterly_results',
    }));
    expect(api.updateNewsLlmSettings).not.toHaveBeenCalled();
  });

  it('shows the current worker article input and processed output', async () => {
    render(<NewsLlm />);

    expect(await screen.findByText('Live Processing Monitor')).toBeInTheDocument();
    expect(await screen.findByText('150,000')).toBeInTheDocument();
    expect(screen.getByText('4,450,000')).toBeInTheDocument();
    expect(screen.getByText('21.7/min')).toBeInTheDocument();
    expect(screen.getByText('Combined historical pass')).toBeInTheDocument();
    expect(await screen.findByText('8d 16h')).toBeInTheDocument();
    expect(await screen.findByText('4,300,000')).toBeInTheDocument();
    expect((await screen.findAllByText('May 1, 2026')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('Nov 9, 2018')).length).toBeGreaterThan(0);
    expect(await screen.findByRole('option', { name: 'Luminar works with NVIDIA' })).toBeInTheDocument();
    expect(screen.getByText(/The companies announced a lidar partnership/)).toBeInTheDocument();
    expect(screen.getByText(/LAZR/)).toBeInTheDocument();
    expect(screen.getAllByText(/NVDA/).length).toBeGreaterThan(0);
    expect(api.getNewsLlmWatch).toHaveBeenCalled();
  });

  it('shows enrichment coverage, queue ETA, and review samples', async () => {
    render(<NewsLlm />);

    expect(await screen.findByText('183,031')).toBeInTheDocument();
    expect(screen.getByText('6.8%')).toBeInTheDocument();
    expect(screen.getByText('10m')).toBeInTheDocument();
    expect(screen.getByText('140,000')).toBeInTheDocument();
    expect(screen.getByText('76.5%')).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Optional spot checks/));
    expect(screen.getByText('NVIDIA announces a partnership')).toBeInTheDocument();
    expect(screen.getByText(/NVIDIA announced a new partnership/)).toBeInTheDocument();
    expect(screen.queryByText('Local weather forecast')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: /unrelated/i }));
    expect(screen.getByText('Local weather forecast')).toBeInTheDocument();
    expect(api.getNewsLlmQuality).toHaveBeenCalledWith(false);
  });

  it('records a human validation and refreshes the sample report', async () => {
    render(<NewsLlm />);
    fireEvent.click(await screen.findByText(/Optional spot checks/));
    fireEvent.change(screen.getByPlaceholderText(/what should change/i), { target: { value: 'Wrong ticker' } });
    fireEvent.click(screen.getByRole('button', { name: /^Incorrect$/i }));

    await waitFor(() => expect(api.reviewNewsLlmSample).toHaveBeenCalledWith(
      expect.objectContaining({ url: 'https://example.com/nvidia', category: 'ticker' }),
      'incorrect',
      'Wrong ticker',
    ));
    await waitFor(() => expect(api.getNewsLlmQuality).toHaveBeenCalledWith(true));
  });

  it('loads a reviewed article into the sentiment benchmark form', async () => {
    render(<NewsLlm />);
    fireEvent.click(await screen.findByText(/Optional spot checks/));
    fireEvent.click(screen.getByRole('button', { name: /Use for sentiment/i }));

    const evaluator = screen.getByRole('region', { name: 'Financial sentiment evaluator' });
    expect(within(evaluator).getByDisplayValue('NVIDIA announces a partnership')).toBeInTheDocument();
    expect(within(evaluator).getByDisplayValue('NVIDIA announced a new partnership.')).toBeInTheDocument();
    expect(within(evaluator).getByDisplayValue('https://example.com/nvidia')).toBeInTheDocument();
  });

  it('replays failed records from the worker status control', async () => {
    render(<NewsLlm />);

    fireEvent.click(await screen.findByRole('button', { name: /retry failed/i }));

    await waitFor(() => expect(api.retryNewsLlmFailures).toHaveBeenCalledWith(500));
    expect(await screen.findByText(/115 failed record\(s\) returned/)).toBeInTheDocument();
  });

  it('searches and previews an isolated financial sentiment model', async () => {
    render(<NewsLlm />);

    expect(await screen.findByText('Financial Sentiment Evaluator')).toBeInTheDocument();
    expect((await screen.findAllByText('NVIDIA GeForce RTX 5060 Ti')).length).toBeGreaterThan(0);
    const evaluator = screen.getByRole('region', { name: 'Financial sentiment evaluator' });
    fireEvent.click(within(evaluator).getByRole('button', { name: /^Search$/i }));
    expect(await screen.findByText(/Financial PhraseBank financial news sentences/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Preview sentiment/i }));
    expect((await screen.findAllByText('positive')).length).toBeGreaterThan(0);
    expect(screen.getByText('91% confidence')).toBeInTheDocument();
    expect(api.previewFinancialSentiment).toHaveBeenCalledWith(expect.objectContaining({
      headline: 'Company raises full-year revenue guidance',
    }));
  });

  it('reports the sentiment runtime device without claiming a GPU', async () => {
    api.getSentimentModelStatus.mockResolvedValueOnce({
      available: true,
      loaded: false,
      cuda: false,
      requested_device: 'cpu',
      device: 'cpu',
      vram_allocated: 0,
    });

    render(<NewsLlm />);

    const evaluator = await screen.findByRole('region', { name: 'Financial sentiment evaluator' });
    expect(within(evaluator).getAllByText('CPU').length).toBeGreaterThan(0);
    expect(within(evaluator).queryByText(/GPU 0/)).not.toBeInTheDocument();
    expect(within(evaluator).getByText('None')).toBeInTheDocument();
  });

  it('saves a labeled sentiment evaluation and reloads model accuracy', async () => {
    render(<NewsLlm />);
    const evaluator = await screen.findByRole('region', { name: 'Financial sentiment evaluator' });
    fireEvent.click(within(evaluator).getByRole('button', { name: 'positive' }));
    fireEvent.click(within(evaluator).getByRole('button', { name: /Evaluate & save/i }));

    await waitFor(() => expect(api.evaluateFinancialSentiment).toHaveBeenCalledWith(
      expect.objectContaining({ headline: 'Company raises full-year revenue guidance' }),
      'positive',
    ));
    expect(await within(evaluator).findByText(/Expected positive: correct/i)).toBeInTheDocument();
    await waitFor(() => expect(api.getSentimentEvaluations).toHaveBeenCalledTimes(2));
  });

  it('saves ground truth independently and runs it against the loaded model', async () => {
    render(<NewsLlm />);
    const evaluator = await screen.findByRole('region', { name: 'Financial sentiment evaluator' });
    fireEvent.click(within(evaluator).getByRole('button', { name: /Save labeled case/i }));
    await waitFor(() => expect(api.saveSentimentBenchmarkCase).toHaveBeenCalledWith(
      expect.objectContaining({ headline: 'Company raises full-year revenue guidance' }),
      'neutral',
    ));

    fireEvent.click(within(evaluator).getByRole('button', { name: /Run saved benchmark/i }));
    await waitFor(() => expect(api.runSentimentBenchmark).toHaveBeenCalledWith(30));
    expect(await screen.findByText(/1 saved sentiment cases evaluated/i)).toBeInTheDocument();
  });

  it('deletes a labeled benchmark case after confirmation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    render(<NewsLlm />);
    const evaluator = await screen.findByRole('region', { name: 'Financial sentiment evaluator' });
    fireEvent.click(within(evaluator).getByRole('button', { name: /Delete benchmark case Company raises/i }));

    await waitFor(() => expect(api.deleteSentimentBenchmarkCase).toHaveBeenCalledWith('case-1'));
    await waitFor(() => expect(api.getSentimentEvaluations).toHaveBeenCalledTimes(2));
    confirm.mockRestore();
  });

  it('exports ground truth without model predictions', async () => {
    render(<NewsLlm />);
    const evaluator = await screen.findByRole('region', { name: 'Financial sentiment evaluator' });
    fireEvent.click(within(evaluator).getByRole('button', { name: /Export JSONL/i }));

    await waitFor(() => expect(api.getSentimentBenchmarkCases).toHaveBeenCalledWith(500, 0));
    await waitFor(() => expect(downloadTextFile).toHaveBeenCalled());
    const [content, filename] = downloadTextFile.mock.calls[0];
    expect(content).toContain('"expected_label":"positive"');
    expect(content).toContain('"note":"Reviewed manually"');
    expect(content).not.toContain('predicted_label');
    expect(filename).toMatch(/^sentiment-benchmark-\d{4}-\d{2}-\d{2}\.jsonl$/);
  });

  it('imports JSONL ground truth and refreshes benchmark totals', async () => {
    render(<NewsLlm />);
    const evaluator = await screen.findByRole('region', { name: 'Financial sentiment evaluator' });
    const input = within(evaluator).getByLabelText('Import sentiment benchmark file');
    const file = new File(['ignored'], 'benchmark.jsonl', { type: 'text/plain' });
    file.text = vi.fn().mockResolvedValue([
      JSON.stringify({ headline: 'Costs declined', summary: 'Margins improved.', expected_label: 'positive' }),
      JSON.stringify({ headline: 'Outlook unchanged', summary: '', expected_label: 'neutral' }),
    ].join('\n'));

    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(api.importSentimentBenchmarkCases).toHaveBeenCalledWith([
      expect.objectContaining({ headline: 'Costs declined', expected_label: 'positive' }),
      expect.objectContaining({ headline: 'Outlook unchanged', expected_label: 'neutral' }),
    ]));
    await waitFor(() => expect(api.getSentimentEvaluations).toHaveBeenCalledTimes(2));
  });
});
