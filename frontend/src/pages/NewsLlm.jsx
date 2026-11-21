import { useCallback, useEffect, useRef, useState } from 'react';
import { formatBatchSeconds, formatBatchTime } from '../utils/newsLlmTiming';
import { FiArrowDownCircle, FiCheck, FiCpu, FiDownload, FiExternalLink, FiEye, FiPlay, FiRefreshCw, FiRotateCcw, FiSearch, FiTrash2, FiUpload, FiX } from 'react-icons/fi';
import {
  deleteNewsLlmModel,
  deleteSentimentBenchmarkCase,
  downloadAcceptedNewsTrainingData,
  evaluateFinancialSentiment,
  getNewsLlmStatus,
  getNewsLlmQuality,
  getNewsLlmWatch,
  getSentimentEvaluations,
  getSentimentBenchmarkCases,
  getSentimentModelStatus,
  importSentimentBenchmarkCases,
  loadSentimentModel,
  previewNewsLlmArticle,
  previewFinancialSentiment,
  pullNewsLlmModel,
  reviewNewsLlmSample,
  retryNewsLlmFailures,
  runSentimentBenchmark,
  saveSentimentBenchmarkCase,
  searchNewsLlmCatalog,
  searchSentimentModels,
  unloadSentimentModel,
  updateNewsLlmSettings,
} from '../lib/api';
import { downloadBlob, downloadTextFile } from '../lib/calendarFiles';

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return 'Unknown size';
  return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
}

function formatWorkerState(value) {
  return String(value || 'not_started').replaceAll('_', ' ');
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (!value) return 'Calculating';
  const days = Math.floor(value / 86400);
  const hours = Math.floor(value / 3600);
  const minutes = Math.max(1, Math.round((value % 3600) / 60));
  if (days) return `${days.toLocaleString()}d ${Math.floor((value % 86400) / 3600)}h`;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function formatCursorDate(timestamp) {
  const value = Number(timestamp || 0);
  if (!value) return 'Calculating';
  return new Date(value * 1000).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

function formatCachedAt(timestamp) {
  const value = Number(timestamp || 0);
  if (!value) return 'Not generated';
  return new Date(value * 1000).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function sentimentDeviceLabel(status) {
  if (!status) return 'Checking device';
  if (status.cuda) return status.gpu_name || status.device || 'CUDA';
  if (status.requested_device === 'cuda') return 'CPU fallback';
  return 'CPU';
}

function companyNames(sample) {
  return (sample.related_companies || []).map((company) => (
    typeof company === 'string' ? company : company.name
  )).filter(Boolean).join(', ');
}

const REVIEW_CATEGORIES = ['ticker', 'relationship', 'translated', 'headline_changed', 'unrelated', 'unverified'];

function companyReviewText(company) {
  if (typeof company === 'string') return company;
  const tickers = company.validated_tickers || [];
  return [company.name, tickers.length ? `(${tickers.join(', ')})` : '', company.evidence ? `: "${company.evidence}"` : '']
    .filter(Boolean)
    .join(' ');
}

function relationshipText(relationship) {
  const left = [relationship.company_a, ...(relationship.validated_tickers_a || [])].filter(Boolean).join(' ');
  const right = [relationship.company_b, ...(relationship.validated_tickers_b || [])].filter(Boolean).join(' ');
  return `${left} ${relationship.relationship || 'related to'} ${right}`.trim();
}

export default function NewsLlm() {
  const [status, setStatus] = useState(null);
  const [query, setQuery] = useState('');
  const [catalog, setCatalog] = useState([]);
  const [modelName, setModelName] = useState('');
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [messageKind, setMessageKind] = useState('success');
  const [previewInput, setPreviewInput] = useState({
    title: 'company_report_quarterly_results',
    summary: 'A public company reported quarterly revenue and updated its outlook.',
    source: 'manual preview',
  });
  const [preview, setPreview] = useState(null);
  const [watch, setWatch] = useState(null);
  const [quality, setQuality] = useState(null);
  const [reviewingSample, setReviewingSample] = useState('');
  const [exportingTrainingData, setExportingTrainingData] = useState(false);
  const [reviewCategory, setReviewCategory] = useState('ticker');
  const [reviewNotes, setReviewNotes] = useState({});
  const [sentimentStatus, setSentimentStatus] = useState(null);
  const [sentimentCatalog, setSentimentCatalog] = useState([]);
  const [sentimentQuery, setSentimentQuery] = useState('financial sentiment');
  const [sentimentModel, setSentimentModel] = useState('');
  const [sentimentLabelMap, setSentimentLabelMap] = useState('');
  const [sentimentBusy, setSentimentBusy] = useState('');
  const [sentimentPreviewInput, setSentimentPreviewInput] = useState({
    headline: 'Company raises full-year revenue guidance',
    summary: 'Management reported stronger demand and improved expected margins.',
    source_url: '',
  });
  const [sentimentPreview, setSentimentPreview] = useState(null);
  const [sentimentExpected, setSentimentExpected] = useState('neutral');
  const [sentimentEvaluation, setSentimentEvaluation] = useState(null);
  const [sentimentEvaluations, setSentimentEvaluations] = useState(null);
  const [watchedArticleId, setWatchedArticleId] = useState('');
  const sentimentSectionRef = useRef(null);
  const sentimentImportRef = useRef(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await getNewsLlmStatus());
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const timer = window.setInterval(loadStatus, 10000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  useEffect(() => {
    let active = true;
    const loadWatch = async () => {
      try {
        const next = await getNewsLlmWatch();
        if (active) setWatch(next);
      } catch {
        if (active) setWatch((current) => current || { phase: 'unavailable', inputs: [], outputs: {} });
      }
    };
    loadWatch();
    const timer = window.setInterval(loadWatch, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const loadQuality = useCallback(async (refresh = false) => {
    try {
      setQuality(await getNewsLlmQuality(refresh));
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    }
  }, []);

  useEffect(() => {
    loadQuality();
    const timer = window.setInterval(loadQuality, 60000);
    return () => window.clearInterval(timer);
  }, [loadQuality]);

  const reviewSample = async (sample, judgment) => {
    const key = `${sample.category}:${sample.url}`;
    setReviewingSample(key);
    setMessage('');
    try {
      await reviewNewsLlmSample(sample, judgment, reviewNotes[key] || '');
      setReviewNotes((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      await loadQuality(true);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setReviewingSample('');
    }
  };

  const retryFailures = async () => {
    setBusy('retry-failed');
    setMessage('');
    try {
      const result = await retryNewsLlmFailures(500);
      await loadStatus();
      setMessageKind('success');
      setMessage(`${Number(result.replayed || 0).toLocaleString()} failed record(s) returned to the processing queue.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const exportTrainingData = async () => {
    setExportingTrainingData(true);
    setMessage('');
    try {
      const blob = await downloadAcceptedNewsTrainingData();
      downloadBlob(blob, `accepted-news-${new Date().toISOString().slice(0, 10)}.jsonl`);
      setMessageKind('success');
      setMessage('Accepted ticker-linked news dataset exported.');
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setExportingTrainingData(false);
    }
  };

  const loadSentimentStatus = useCallback(async () => {
    try {
      setSentimentStatus(await getSentimentModelStatus());
    } catch (error) {
      setSentimentStatus({ available: false, loaded: false, error: error.message });
    }
  }, []);

  useEffect(() => {
    loadSentimentStatus();
  }, [loadSentimentStatus]);

  const loadSentimentEvaluations = useCallback(async () => {
    try {
      setSentimentEvaluations(await getSentimentEvaluations(20));
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    }
  }, []);

  useEffect(() => {
    loadSentimentEvaluations();
  }, [loadSentimentEvaluations]);

  const searchSentiment = async () => {
    setSentimentBusy('search');
    setMessage('');
    try {
      const result = await searchSentimentModels(sentimentQuery);
      setSentimentCatalog(result.models || []);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const activateSentiment = async (model = sentimentModel) => {
    const modelId = String(model || '').trim();
    if (!modelId) return;
    let labelMap = {};
    try {
      labelMap = sentimentLabelMap.trim() ? JSON.parse(sentimentLabelMap) : {};
    } catch {
      setMessageKind('error');
      setMessage('Custom label map must be valid JSON.');
      return;
    }
    setSentimentBusy(`load:${modelId}`);
    setMessageKind('success');
    setMessage(`Downloading and loading ${modelId}.`);
    try {
      setSentimentStatus(await loadSentimentModel(modelId, labelMap));
      setSentimentModel(modelId);
      setMessage(`${modelId} is loaded for preview evaluation only.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const removeSentiment = async () => {
    setSentimentBusy('unload');
    try {
      setSentimentStatus(await unloadSentimentModel());
      setSentimentPreview(null);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const runSentimentPreview = async () => {
    setSentimentBusy('preview');
    setSentimentPreview(null);
    try {
      setSentimentPreview(await previewFinancialSentiment(sentimentPreviewInput));
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const runSentimentEvaluation = async () => {
    setSentimentBusy('evaluate');
    setSentimentEvaluation(null);
    try {
      const result = await evaluateFinancialSentiment(sentimentPreviewInput, sentimentExpected);
      setSentimentEvaluation(result);
      setSentimentPreview({ ...result, label: result.predicted_label || result.label });
      await loadSentimentEvaluations();
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const saveSentimentCase = async () => {
    setSentimentBusy('save-case');
    try {
      await saveSentimentBenchmarkCase(sentimentPreviewInput, sentimentExpected);
      await loadSentimentEvaluations();
      setMessageKind('success');
      setMessage('Sentiment benchmark case saved.');
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const runSavedSentimentBenchmark = async () => {
    setSentimentBusy('benchmark');
    try {
      const result = await runSentimentBenchmark(30);
      setSentimentEvaluations(result.summary);
      setMessageKind('success');
      setMessage(`${Number(result.evaluated || 0).toLocaleString()} saved sentiment cases evaluated.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const loadArticleForSentiment = (sample) => {
    setSentimentPreviewInput({
      headline: sample.display_headline || sample.original_title || '',
      summary: sample.llm_summary || sample.relevance_reason || '',
      source_url: sample.url || '',
    });
    setSentimentPreview(null);
    setSentimentEvaluation(null);
    sentimentSectionRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  };

  const removeSentimentCase = async (row) => {
    if (!window.confirm(`Delete the ${row.expected_label} benchmark case "${row.headline}"?`)) return;
    setSentimentBusy(`delete-case:${row.case_key}`);
    try {
      await deleteSentimentBenchmarkCase(row.case_key);
      await loadSentimentEvaluations();
      setMessageKind('success');
      setMessage('Sentiment benchmark case deleted.');
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const exportSentimentCases = async () => {
    setSentimentBusy('export-cases');
    try {
      const cases = [];
      let offset = 0;
      while (true) {
        const result = await getSentimentBenchmarkCases(500, offset);
        const batch = result.cases || [];
        cases.push(...batch);
        if (batch.length < 500) break;
        offset += batch.length;
      }
      if (!cases.length) throw new Error('There are no sentiment benchmark cases to export.');
      const jsonl = cases.map((row) => JSON.stringify({
        headline: row.headline,
        summary: row.summary || '',
        expected_label: row.expected_label,
        source_url: row.source_url || '',
        note: row.note || '',
      })).join('\n');
      downloadTextFile(`${jsonl}\n`, `sentiment-benchmark-${new Date().toISOString().slice(0, 10)}.jsonl`);
      setMessageKind('success');
      setMessage(`${cases.length.toLocaleString()} sentiment benchmark case(s) exported.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setSentimentBusy('');
    }
  };

  const importSentimentCases = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setSentimentBusy('import-cases');
    try {
      const text = (await file.text()).trim();
      if (!text) throw new Error('The selected benchmark file is empty.');
      let parsed;
      if (text.startsWith('[') || text.startsWith('{')) {
        try {
          const json = JSON.parse(text);
          parsed = Array.isArray(json) ? json : json.cases;
        } catch {
          parsed = text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
        }
      } else {
        parsed = text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
      }
      if (!Array.isArray(parsed) || !parsed.length) throw new Error('The file contains no benchmark cases.');
      let imported = 0;
      for (let start = 0; start < parsed.length; start += 500) {
        const result = await importSentimentBenchmarkCases(parsed.slice(start, start + 500));
        imported += Number(result.imported || 0);
      }
      await loadSentimentEvaluations();
      setMessageKind('success');
      setMessage(`${imported.toLocaleString()} sentiment benchmark case(s) imported or updated.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(`Benchmark import failed: ${error.message}`);
    } finally {
      setSentimentBusy('');
    }
  };

  useEffect(() => {
    const inputs = watch?.inputs || [];
    if (inputs.length && !inputs.some((item) => item.id === watchedArticleId)) {
      setWatchedArticleId(inputs[0].id);
    }
  }, [watch, watchedArticleId]);

  const search = async () => {
    setBusy('search');
    setMessage('');
    try {
      const result = await searchNewsLlmCatalog(query);
      setCatalog(result.models || []);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const pull = async (name = modelName) => {
    const model = String(name || '').trim();
    if (!model) return;
    setBusy(`pull:${model}`);
    setMessageKind('success');
    setMessage(`Pulling ${model}. This can take several minutes.`);
    try {
      await pullNewsLlmModel(model);
      setMessageKind('success');
      setMessage(`${model} is installed and selected. News processing remains paused.`);
      await loadStatus();
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const activate = async (model) => {
    setBusy(`activate:${model}`);
    setMessage('');
    try {
      await updateNewsLlmSettings({ active_model: model });
      await loadStatus();
      setMessageKind('success');
      setMessage(`${model} is selected. News processing remains paused until you enable it.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const remove = async (model) => {
    setBusy(`delete:${model}`);
    setMessage('');
    try {
      await deleteNewsLlmModel(model);
      await loadStatus();
      setMessageKind('success');
      setMessage(`${model} was removed.`);
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const setEnabled = async (enabled) => {
    setBusy('enabled');
    setMessage('');
    try {
      await updateNewsLlmSettings({ enabled });
      await loadStatus();
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const runPreview = async () => {
    setBusy('preview');
    setMessage('');
    setPreview(null);
    try {
      setPreview(await previewNewsLlmArticle(previewInput));
    } catch (error) {
      setMessageKind('error');
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const watchedInput = (watch?.inputs || []).find((item) => item.id === watchedArticleId)
    || (watch?.inputs || [])[0];
  const watchedOutput = watchedInput ? watch?.outputs?.[watchedInput.id] : null;
  const reviewSamples = (quality?.samples || []).filter((sample) => sample.category === reviewCategory);
  const historicalOperations = status?.historical_operations;
  const sharedHistoricalQueue = historicalOperations?.queue_mode === 'shared_newest_first';

  return (
    <section className="page-stack news-llm-page">
      <header className="panel news-llm-header">
        <div>
          <h2 className="page-title"><FiCpu /> News LLM</h2>
          <p className="section-copy">Ollama model used for financial relevance, headlines, companies, and tickers.</p>
        </div>
        <button type="button" className="scanner-button" onClick={loadStatus}>
          <FiRefreshCw /> Refresh
        </button>
      </header>

      <section className="news-llm-status" aria-label="News LLM status">
        <div><span>Ollama</span><strong>{status?.available ? `Online ${status.version || ''}` : 'Unavailable'}</strong></div>
        <div><span>Endpoint</span><strong>localhost:{status?.host_port || 11435}</strong></div>
        <div><span>GPU</span><strong>GPU {status?.gpu_device ?? '0'} only</strong></div>
        <div><span>Active model</span><strong>{status?.active_model || 'None'}</strong></div>
        <label className="scanner-toggle">
          <input
            type="checkbox"
            checked={Boolean(status?.enabled)}
            disabled={!status || busy === 'enabled'}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          Process incoming news
        </label>
      </section>

      <section className="news-llm-history-status" aria-label="Historical article processing">
        <div className="news-llm-section-heading">
          <div>
            <h3>Backlog &amp; Historical Pass</h3>
            <p>A one-shot worker finishes the retained backlog, then processes canonical historical articles and exits.</p>
          </div>
          <span className="status-pill status-pill-muted">
            {formatWorkerState(status?.historical_backfill?.status)}
          </span>
        </div>
        <div className="news-llm-history-overview" aria-label="Combined historical progress">
          <div className="news-llm-history-progress-copy">
            <span>{sharedHistoricalQueue ? 'Shared newest-first queue' : 'Combined historical pass'}</span>
            <strong>{Number(historicalOperations?.progress_percent || 0).toFixed(1)}%</strong>
          </div>
          <progress
            aria-label="Combined historical completion"
            max="100"
            value={Number(historicalOperations?.progress_percent || 0)}
          />
          <div className="news-llm-history-metrics">
            <div><span>Processed</span><strong>{Number(historicalOperations?.completed || 0).toLocaleString()}</strong></div>
            <div><span>{sharedHistoricalQueue ? 'Remaining scan estimate' : 'Remaining'}</span><strong>{Number(historicalOperations?.remaining || 0).toLocaleString()}</strong></div>
            <div><span>{sharedHistoricalQueue ? 'Recent batch rate' : 'Combined throughput'}</span><strong>{Number(historicalOperations?.rate_per_minute || 0).toFixed(1)}/min</strong></div>
            {sharedHistoricalQueue ? <>
              <div><span>Sustained completions</span><strong>{historicalOperations?.sustained_rate_per_minute == null ? 'Pending' : `${Number(historicalOperations.sustained_rate_per_minute).toFixed(1)}/min`}</strong></div>
              <div><span>Measured completions</span><strong>{historicalOperations?.measured_completions == null ? 'Pending' : Number(historicalOperations.measured_completions).toLocaleString()}</strong></div>
              <div><span>Observation window</span><strong>{formatDuration(historicalOperations?.tracking_elapsed_seconds)}</strong></div>
            </> : <div><span>Parallel backlog estimate</span><strong>{formatDuration(historicalOperations?.parallel_eta_seconds)}</strong></div>}
            <div><span>{sharedHistoricalQueue ? 'Worker 1 date' : 'Recent cursor'}</span><strong>{formatCursorDate(historicalOperations?.workers?.recent?.cursor_published_ts)}</strong></div>
            <div><span>{sharedHistoricalQueue ? 'Worker 2 date' : 'Older cursor'}</span><strong>{formatCursorDate(historicalOperations?.workers?.older?.cursor_published_ts)}</strong></div>
          </div>
          {!sharedHistoricalQueue && <p className="news-llm-partition-note">
            Recent and older workers meet at {formatCursorDate(historicalOperations?.partition_boundary_ts)}. The finish estimate uses the slower parallel partition.
          </p>}
        </div>
        <div className="news-llm-history-metrics">
          <div><span>Backlog completed</span><strong>{Number(status?.historical_backfill?.calendar_completed || 0).toLocaleString()}</strong></div>
          <div><span>Historical completed</span><strong>{Number(status?.historical_backfill?.historical_completed || status?.historical_backfill?.completed || 0).toLocaleString()}</strong></div>
          <div><span>Historical remaining</span><strong>{Number(status?.historical_backfill?.historical_remaining || 0).toLocaleString()}</strong></div>
          <div><span>Reached date</span><strong>{formatCursorDate(status?.historical_backfill?.historical_cursor_published_ts)}</strong></div>
          <div><span>Backlog pass estimate</span><strong>{formatDuration(status?.historical_backfill?.historical_eta_seconds)}</strong></div>
          <div><span>Current batch</span><strong>{Number(status?.historical_backfill?.batch_size || 0).toLocaleString()}</strong></div>
          <div><span>Throughput</span><strong>{Number(status?.historical_backfill?.historical_rate_per_minute ?? status?.historical_backfill?.records_per_minute ?? 0).toFixed(1)}/min</strong></div>
          <div><span>Last batch completed</span><strong>{formatBatchTime(status?.historical_backfill?.last_forward_batch)}</strong></div>
          <div><span>Last batch selection</span><strong>{formatBatchSeconds(status?.historical_backfill?.last_forward_batch, 'selection_seconds')}</strong></div>
          <div><span>Last batch publisher wait</span><strong>{formatBatchSeconds(status?.historical_backfill?.last_forward_batch, 'resolve_seconds')}</strong></div>
          <div><span>Last batch inference</span><strong>{formatBatchSeconds(status?.historical_backfill?.last_forward_batch, 'inference_seconds')}</strong></div>
          <div><span>Last batch persistence</span><strong>{formatBatchSeconds(status?.historical_backfill?.last_forward_batch, 'persist_seconds')}</strong></div>
        </div>
        <div className="news-llm-queue-summary" aria-label={sharedHistoricalQueue ? 'Historical worker 2' : 'Older historical partition'}>
          <span>{sharedHistoricalQueue ? 'Worker 2 ' : 'Older partition '}<strong>{formatWorkerState(status?.historical_helper?.status)}</strong></span>
          <span>Completed <strong>{Number(status?.historical_helper?.historical_completed || status?.historical_helper?.completed || 0).toLocaleString()}</strong></span>
          <span>Remaining <strong>{Number(status?.historical_helper?.historical_remaining || 0).toLocaleString()}</strong></span>
          <span>{sharedHistoricalQueue ? 'Working date ' : 'Starts before '}<strong>{formatCursorDate(status?.historical_helper?.historical_cursor_published_ts)}</strong></span>
          <span>Backlog pass estimate <strong>{formatDuration(status?.historical_helper?.historical_eta_seconds)}</strong></span>
          <span>Current batch <strong>{Number(status?.historical_helper?.batch_size || 0).toLocaleString()}</strong></span>
          <span>Throughput <strong>{Number(status?.historical_helper?.historical_rate_per_minute ?? status?.historical_helper?.records_per_minute ?? 0).toFixed(1)}/min</strong></span>
          <span>Last batch completed <strong>{formatBatchTime(status?.historical_helper?.last_forward_batch)}</strong></span>
          <span>Last batch inference <strong>{formatBatchSeconds(status?.historical_helper?.last_forward_batch, 'inference_seconds')}</strong></span>
          <span>Last batch persistence <strong>{formatBatchSeconds(status?.historical_helper?.last_forward_batch, 'persist_seconds')}</strong></span>
        </div>
      </section>

      <section className="news-llm-section" aria-label="News LLM quality and coverage">
        <div className="news-llm-section-heading">
          <div>
            <h3>Quality &amp; Coverage</h3>
            <p>Automated acceptance uses grounded source content, validated securities, explicit evidence, and model confidence.</p>
          </div>
          <div className="news-llm-heading-actions">
            <button type="button" className="scanner-button" disabled={exportingTrainingData} onClick={exportTrainingData}>
              <FiDownload /> {exportingTrainingData ? 'Exporting...' : 'Export accepted JSONL'}
            </button>
            <button type="button" className="scanner-button" onClick={() => loadQuality(true)}>
              <FiRefreshCw /> Refresh metrics
            </button>
          </div>
        </div>
        {quality?.summary?.available !== false && <div className="news-llm-quality-metrics">
          <div><span>Enriched records</span><strong>{Number(quality?.summary?.completed || 0).toLocaleString()}</strong></div>
          <div><span>Validated tickers</span><strong>{Number(quality?.summary?.coverage?.tickers_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Companies</span><strong>{Number(quality?.summary?.coverage?.companies_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Relationships</span><strong>{Number(quality?.summary?.coverage?.relationships_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Publisher verified</span><strong>{Number(quality?.summary?.coverage?.canonical_verified_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Retry queue</span><strong>{Number(quality?.summary?.retry_pending || 0).toLocaleString()}</strong></div>
        </div>}
        <div className="news-llm-queue-summary">
          <span>Live queue <strong>{Number(status?.live_worker?.live_pending || 0).toLocaleString()}</strong></span>
          <span>Backlog <strong>{Number(status?.live_worker?.backlog_pending || 0).toLocaleString()}</strong></span>
          <span>Throughput <strong>{Number(status?.live_worker?.records_per_minute || 0).toFixed(1)}/min</strong></span>
          <span>Estimated queue time <strong>{formatDuration(status?.live_worker?.eta_seconds)}</strong></span>
          <span>Failed <strong>{Number(status?.live_worker?.failed || 0).toLocaleString()}</strong></span>
          <span>Metrics cached <strong>{formatCachedAt(quality?.cache?.generated_at)}</strong></span>
          {quality?.cache?.refresh_required && <span><strong>{quality?.summary?.available === false ? 'Metrics not generated' : 'Metrics outdated'}</strong></span>}
          {quality?.cache?.refresh_in_progress && <span><strong>Metrics refresh running</strong></span>}
          {quality?.cache?.refresh_failed && <span className="state-error"><strong>Last refresh failed</strong></span>}
          {Number(status?.live_worker?.failed || 0) > 0 && <button
            type="button"
            className="scanner-button"
            disabled={busy === 'retry-failed'}
            onClick={retryFailures}
          >
            <FiRotateCcw /> {busy === 'retry-failed' ? 'Queuing...' : 'Retry failed'}
          </button>}
        </div>
        {quality?.summary?.available !== false && <div className="news-llm-quality-metrics" aria-label="Automated acceptance">
          <div><span>Automatically accepted</span><strong>{Number(quality?.summary?.automated_acceptance?.accepted || 0).toLocaleString()}</strong></div>
          <div><span>Acceptance rate</span><strong>{Number(quality?.summary?.automated_acceptance?.accepted_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Source grounded</span><strong>{Number(quality?.summary?.automated_acceptance?.source_grounded_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Confidence passed</span><strong>{Number(quality?.summary?.automated_acceptance?.confidence_passed_percent || 0).toFixed(1)}%</strong></div>
          <div><span>Provisional</span><strong>{Number(quality?.summary?.automated_acceptance?.provisional || 0).toLocaleString()}</strong></div>
          <div><span>Needs attention</span><strong>{Number(quality?.summary?.automated_acceptance?.needs_attention || 0).toLocaleString()}</strong></div>
        </div>}
        <div className="news-llm-queue-summary" aria-label="Historical entity relink">
          <span>Entity relink <strong>{formatWorkerState(status?.entity_relink?.status)}</strong></span>
          <span>Completed <strong>{Number(status?.entity_relink?.completed || 0).toLocaleString()}</strong></span>
          <span>Remaining <strong>{Number(status?.entity_relink?.remaining || 0).toLocaleString()}</strong></span>
          <span>Ticker sets changed <strong>{Number(status?.entity_relink?.changed || 0).toLocaleString()}</strong></span>
          <span>Recent cache synced <strong>{Number(status?.entity_relink?.synced_recent || 0).toLocaleString()}</strong></span>
          <span>Accepted symbols <strong>{Number(status?.entity_relink?.accepted_symbols || 0).toLocaleString()}</strong></span>
        </div>
        {(quality?.samples || []).length > 0 && <details className="news-llm-quality-samples">
          <summary>Optional spot checks ({quality.samples.length})</summary>
          <div className="news-llm-review-tabs" role="tablist" aria-label="Review category">
            {REVIEW_CATEGORIES.map((category) => <button
              key={category}
              type="button"
              role="tab"
              aria-selected={reviewCategory === category}
              className={reviewCategory === category ? 'active' : ''}
              onClick={() => setReviewCategory(category)}
            >
              {formatWorkerState(category)} ({(quality.samples || []).filter((sample) => sample.category === category).length})
            </button>)}
          </div>
          <div>
            {reviewSamples.map((sample, index) => {
              const sampleKey = `${sample.category}:${sample.url}`;
              return <article key={`${sampleKey}:${index}`}>
              <span>{formatWorkerState(sample.category)}</span>
              <a href={sample.url} target="_blank" rel="noreferrer">{sample.display_headline || sample.original_title || sample.url}</a>
              <div className="news-llm-sample-context">
                {sample.original_title && sample.original_title !== sample.display_headline && <div><b>Original</b><span>{sample.original_title}</span></div>}
                {(sample.related_tickers || []).length > 0 && <div><b>Tickers</b><span>{sample.related_tickers.join(', ')}</span></div>}
                {companyNames(sample) && <div><b>Companies</b><span>{(sample.related_companies || []).map(companyReviewText).join('; ')}</span></div>}
                {(sample.company_relationships || []).map((relationship, relationshipIndex) => <div key={`${sampleKey}:relationship:${relationshipIndex}`}>
                  <b>Relationship</b>
                  <span>{relationshipText(relationship)}{relationship.evidence ? `: "${relationship.evidence}"` : ''}</span>
                </div>)}
                {sample.llm_summary && <div><b>Summary</b><span>{sample.llm_summary}</span></div>}
                {sample.relevance_reason && <div><b>Rationale</b><span>{sample.relevance_reason}</span></div>}
                {(sample.llm_topics || []).length > 0 && <div><b>Topics</b><span>{sample.llm_topics.join(', ')}</span></div>}
                {(sample.related_commodities || []).length > 0 && <div><b>Commodities</b><span>{sample.related_commodities.join(', ')}</span></div>}
                <div><b>Classification</b><span>{sample.relevance_label || 'unknown'} · {Math.round(Number(sample.relevance_confidence || 0) * 100)}%</span></div>
                <div><b>Verification</b><span>{formatWorkerState(sample.verification_status)} · {formatWorkerState(sample.headline_source)}</span></div>
              </div>
              <label className="news-llm-review-note">
                <span>Correction note</span>
                <input
                  value={reviewNotes[sampleKey] || ''}
                  onChange={(event) => setReviewNotes((current) => ({ ...current, [sampleKey]: event.target.value }))}
                  placeholder="Optional: what should change?"
                  maxLength={1000}
                />
              </label>
              <div className="news-llm-sample-actions">
                <button type="button" onClick={() => loadArticleForSentiment(sample)} title="Load this article into the sentiment benchmark"><FiArrowDownCircle /> Use for sentiment</button>
                <button type="button" onClick={() => reviewSample(sample, 'correct')} disabled={reviewingSample === sampleKey} title="Mark this result correct"><FiCheck /> Correct</button>
                <button type="button" onClick={() => reviewSample(sample, 'incorrect')} disabled={reviewingSample === sampleKey} title="Mark this result incorrect"><FiX /> Incorrect</button>
              </div>
            </article>})}
          </div>
        </details>}
      </section>

      {status?.error && <div className="state-error">{status.error}</div>}
      {message && <div className={messageKind === 'error' ? 'state-error' : 'settings-feedback settings-feedback-success'} role="status">{message}</div>}

      <section ref={sentimentSectionRef} className="news-llm-section sentiment-evaluator" aria-label="Financial sentiment evaluator">
        <div className="news-llm-section-heading">
          <div>
            <h3>Financial Sentiment Evaluator</h3>
            <p>Preview-only Hugging Face classifier. Results do not affect news records or trades.</p>
          </div>
          <div className="news-llm-watch-state">
            <span className={`status-pill ${sentimentStatus?.available ? 'status-pill-ok' : 'status-pill-error'}`}>
              {sentimentStatus?.available ? 'online' : 'offline'}
            </span>
            <span>{sentimentDeviceLabel(sentimentStatus)}</span>
          </div>
        </div>
        {sentimentStatus?.error && <div className="state-error">{sentimentStatus.error}</div>}
        {sentimentStatus?.cuda_error && <div className="state-error">GPU validation failed: {sentimentStatus.cuda_error}</div>}
        <div className="sentiment-model-status">
          <div><span>Loaded model</span><strong>{sentimentStatus?.model || 'None'}</strong></div>
          <div><span>Device</span><strong>{sentimentDeviceLabel(sentimentStatus)}</strong></div>
          <div><span>VRAM allocated</span><strong>{formatBytes(sentimentStatus?.vram_allocated)}</strong></div>
          <button type="button" onClick={removeSentiment} disabled={!sentimentStatus?.loaded || Boolean(sentimentBusy)}><FiTrash2 /> Unload</button>
        </div>
        <div className="sentiment-model-entry">
          <input value={sentimentModel} onChange={(event) => setSentimentModel(event.target.value)} placeholder="Hugging Face owner/model" />
          <button type="button" onClick={() => activateSentiment()} disabled={!sentimentModel.trim() || Boolean(sentimentBusy)}><FiDownload /> Load</button>
          <textarea rows="2" value={sentimentLabelMap} onChange={(event) => setSentimentLabelMap(event.target.value)} placeholder='Optional custom label map, e.g. {"LABEL_0":"negative","LABEL_1":"neutral","LABEL_2":"positive"}' />
        </div>
        <div className="news-llm-search">
          <input value={sentimentQuery} onChange={(event) => setSentimentQuery(event.target.value)} placeholder="Search Hugging Face models" />
          <button type="button" onClick={searchSentiment} disabled={Boolean(sentimentBusy)}><FiSearch /> {sentimentBusy === 'search' ? 'Searching' : 'Search'}</button>
        </div>
        {sentimentCatalog.length > 0 && <div className="sentiment-catalog">
          {sentimentCatalog.map((model) => <article key={model.id}>
            <div><a href={model.url} target="_blank" rel="noreferrer">{model.id} <FiExternalLink /></a><span>{model.downloads.toLocaleString()} downloads · {model.likes.toLocaleString()} likes · {model.has_safetensors ? 'safetensors' : 'legacy weights'}</span></div>
            {model.training && <p>{model.training}</p>}
            {model.limitation && <p>{model.limitation}</p>}
            <button type="button" onClick={() => activateSentiment(model.id)} disabled={!model.load_allowed || Boolean(sentimentBusy)}>{model.load_allowed ? 'Load' : 'Safetensors required'}</button>
          </article>)}
        </div>}
        <div className="sentiment-preview">
          <label>Headline<input value={sentimentPreviewInput.headline} onChange={(event) => setSentimentPreviewInput((value) => ({ ...value, headline: event.target.value }))} /></label>
          <label>Summary<textarea rows="3" value={sentimentPreviewInput.summary} onChange={(event) => setSentimentPreviewInput((value) => ({ ...value, summary: event.target.value }))} /></label>
          <label>Source URL<input value={sentimentPreviewInput.source_url} onChange={(event) => setSentimentPreviewInput((value) => ({ ...value, source_url: event.target.value }))} /></label>
          <div className="sentiment-preview-actions">
            <button type="button" onClick={runSentimentPreview} disabled={!sentimentStatus?.loaded || Boolean(sentimentBusy)}><FiPlay /> {sentimentBusy === 'preview' ? 'Running' : 'Preview sentiment'}</button>
            <div className="sentiment-expected" role="group" aria-label="Expected sentiment">
              {['negative', 'neutral', 'positive'].map((label) => <button
                key={label}
                type="button"
                aria-pressed={sentimentExpected === label}
                className={sentimentExpected === label ? 'active' : ''}
                onClick={() => setSentimentExpected(label)}
              >{label}</button>)}
            </div>
            <button type="button" onClick={saveSentimentCase} disabled={Boolean(sentimentBusy)}><FiDownload /> {sentimentBusy === 'save-case' ? 'Saving' : 'Save labeled case'}</button>
            <button type="button" onClick={runSentimentEvaluation} disabled={!sentimentStatus?.loaded || Boolean(sentimentBusy)}><FiCheck /> {sentimentBusy === 'evaluate' ? 'Saving' : 'Evaluate & save'}</button>
            <button type="button" onClick={runSavedSentimentBenchmark} disabled={!sentimentStatus?.loaded || !Number(sentimentEvaluations?.case_total || 0) || Boolean(sentimentBusy)}><FiPlay /> {sentimentBusy === 'benchmark' ? 'Running' : 'Run saved benchmark'}</button>
          </div>
          {sentimentPreview && <div className="sentiment-preview-result">
            <strong>{sentimentPreview.label}</strong>
            <span>{Math.round(Number(sentimentPreview.confidence || 0) * 100)}% confidence</span>
            <span>Score {Number(sentimentPreview.score || 0).toFixed(3)}</span>
            {sentimentEvaluation && <span className={sentimentEvaluation.correct ? 'sentiment-correct' : 'sentiment-incorrect'}>
              Expected {sentimentEvaluation.expected_label}: {sentimentEvaluation.correct ? 'correct' : 'incorrect'}
            </span>}
          </div>}
        </div>
        <div className="sentiment-evaluation-summary">
          <span>Labeled cases <strong>{Number(sentimentEvaluations?.case_total || 0).toLocaleString()}</strong></span>
          <span>Negative <strong>{Number(sentimentEvaluations?.case_labels?.negative || 0).toLocaleString()}</strong></span>
          <span>Neutral <strong>{Number(sentimentEvaluations?.case_labels?.neutral || 0).toLocaleString()}</strong></span>
          <span>Positive <strong>{Number(sentimentEvaluations?.case_labels?.positive || 0).toLocaleString()}</strong></span>
          <span>Saved evaluations <strong>{Number(sentimentEvaluations?.total || 0).toLocaleString()}</strong></span>
          {(sentimentEvaluations?.models || []).map((model) => <span key={model.model}>
            {model.model} <strong>{Number(model.accuracy_percent || 0).toFixed(1)}%</strong>
            {' '}({model.correct}/{model.total})
          </span>)}
        </div>
        <div className="sentiment-preview-actions">
          <button type="button" onClick={exportSentimentCases} disabled={!Number(sentimentEvaluations?.case_total || 0) || Boolean(sentimentBusy)}><FiDownload /> Export JSONL</button>
          <button type="button" onClick={() => sentimentImportRef.current?.click()} disabled={Boolean(sentimentBusy)}><FiUpload /> Import JSONL</button>
          <input
            ref={sentimentImportRef}
            type="file"
            accept=".jsonl,.json,application/json,text/plain"
            hidden
            onChange={importSentimentCases}
            aria-label="Import sentiment benchmark file"
          />
        </div>
        {(sentimentEvaluations?.recent_cases || []).length > 0 && <div className="sentiment-benchmark-cases" aria-label="Saved sentiment benchmark cases">
          {(sentimentEvaluations.recent_cases || []).slice(0, 20).map((row) => <div key={row.case_key}>
            <span>{row.headline}</span>
            <strong>{row.expected_label}</strong>
            {row.source_url ? <a href={row.source_url} target="_blank" rel="noreferrer" title="Open source article"><FiExternalLink /></a> : <span />}
            <button
              type="button"
              onClick={() => removeSentimentCase(row)}
              disabled={sentimentBusy === `delete-case:${row.case_key}`}
              title="Delete benchmark case"
              aria-label={`Delete benchmark case ${row.headline}`}
            ><FiTrash2 /></button>
          </div>)}
        </div>}
        {(sentimentEvaluations?.recent || []).length > 0 && <div className="sentiment-evaluation-history">
          {(sentimentEvaluations.recent || []).slice(0, 10).map((row) => <div key={row.evaluation_key}>
            <span>{row.headline}</span>
            <span>{row.expected_label} → {row.predicted_label}</span>
            <strong className={row.correct ? 'sentiment-correct' : 'sentiment-incorrect'}>{row.correct ? 'Correct' : 'Incorrect'}</strong>
          </div>)}
        </div>}
      </section>

      <section className="news-llm-section">
        <div className="news-llm-section-heading">
          <div>
            <h3><FiEye /> Live Processing Monitor</h3>
            <p>Current worker input and the normalized result used by the News Feed.</p>
          </div>
          <div className="news-llm-watch-state">
            <span className={`status-pill ${watch?.phase === 'error' ? 'status-pill-error' : 'status-pill-ok'}`}>
              {watch?.phase || 'loading'}
            </span>
            {watch?.queue && <span>{watch.queue} queue</span>}
            {watch?.updated_at && <time dateTime={new Date(watch.updated_at * 1000).toISOString()}>
              {new Date(watch.updated_at * 1000).toLocaleTimeString()}
            </time>}
          </div>
        </div>
        {(watch?.inputs || []).length > 0 ? <>
          <label className="news-llm-watch-selector">
            <span>Article in batch ({watch.batch_size || watch.inputs.length})</span>
            <select value={watchedInput?.id || ''} onChange={(event) => setWatchedArticleId(event.target.value)}>
              {watch.inputs.map((item) => (
                <option key={item.id} value={item.id}>{item.title || item.id}</option>
              ))}
            </select>
          </label>
          <div className="news-llm-watch-grid">
            <div>
              <span>Model input</span>
              <pre>{JSON.stringify(watchedInput, null, 2)}</pre>
            </div>
            <div>
              <span>Processed output</span>
              <pre>{watchedOutput
                ? JSON.stringify(watchedOutput, null, 2)
                : watch?.phase === 'error'
                  ? watch.error || 'Inference failed.'
                  : 'Waiting for model output...'}</pre>
            </div>
          </div>
          {(watch.failures || []).length > 0 && <div className="state-error">
            {watch.failures.length} article(s) failed in this batch.
          </div>}
        </> : <div className="empty-panel">Waiting for the worker to begin an article batch.</div>}
      </section>

      <section className="news-llm-section">
        <div className="news-llm-section-heading">
          <div>
            <h3>Test Active Model</h3>
            <p>Preview one article while automatic processing remains off.</p>
          </div>
        </div>
        <div className="news-llm-preview-inputs">
          <label>
            Headline
            <input
              value={previewInput.title}
              onChange={(event) => setPreviewInput((value) => ({ ...value, title: event.target.value }))}
            />
          </label>
          <label>
            Article summary or excerpt
            <textarea
              rows="4"
              value={previewInput.summary}
              onChange={(event) => setPreviewInput((value) => ({ ...value, summary: event.target.value }))}
            />
          </label>
          <button type="button" onClick={runPreview} disabled={Boolean(busy) || !status?.active_model_installed}>
            <FiPlay /> {busy === 'preview' ? 'Running' : 'Run preview'}
          </button>
        </div>
        {preview && <div className="news-llm-preview-result">
          <div><span>Display headline</span><strong>{preview.display_headline}</strong></div>
          <div><span>Classification</span><strong>{preview.relevance_label} · {Math.round((preview.relevance_confidence || 0) * 100)}%</strong></div>
          <div><span>Companies</span><strong>{(preview.related_companies || []).map((row) => row.name).join(', ') || 'None'}</strong></div>
          <div><span>Validated tickers</span><strong>{(preview.related_tickers || []).join(', ') || 'None'}</strong></div>
          <div className="news-llm-preview-wide"><span>Summary</span><strong>{preview.llm_summary || 'None'}</strong></div>
          <div className="news-llm-preview-wide"><span>Reason</span><strong>{preview.relevance_reason || 'None'}</strong></div>
        </div>}
      </section>

      <section className="news-llm-section">
        <div className="news-llm-section-heading">
          <div>
            <h3>Installed Models</h3>
            <p>The active model changes immediately; the worker does not need rebuilding.</p>
          </div>
        </div>
        <div className="news-llm-model-list">
          {(status?.models || []).map((model) => {
            const name = model.name || model.model;
            const active = name === status.active_model;
            const running = (status.running_models || []).some((row) => (row.name || row.model) === name);
            return (
              <div className="news-llm-model-row" key={name}>
                <div>
                  <strong>{name}</strong>
                  <span>{model.details?.parameter_size || 'Unknown parameters'} · {model.details?.quantization_level || 'Unknown quantization'} · {formatBytes(model.size)}</span>
                </div>
                <div className="news-llm-model-state">
                  {active && <span className="status-pill status-pill-ok">Active</span>}
                  {running && <span className="status-pill">Loaded</span>}
                </div>
                <button type="button" onClick={() => activate(name)} disabled={active || Boolean(busy)} title={`Use ${name}`}>Use</button>
                <button type="button" onClick={() => remove(name)} disabled={active || Boolean(busy)} title={`Delete ${name}`} aria-label={`Delete ${name}`}><FiTrash2 /></button>
              </div>
            );
          })}
          {status && !(status.models || []).length && <div className="empty-panel">No models installed yet.</div>}
        </div>
      </section>

      <section className="news-llm-section">
        <div className="news-llm-section-heading">
          <div>
            <h3>Find And Install</h3>
            <p>Search the Ollama library or enter an exact model name.</p>
          </div>
          <a href="https://ollama.com/library" target="_blank" rel="noreferrer">Ollama library <FiExternalLink /></a>
        </div>
        <div className="news-llm-search">
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search Ollama models" />
          <button type="button" onClick={search} disabled={busy === 'search'}><FiSearch /> {busy === 'search' ? 'Searching' : 'Search'}</button>
        </div>
        <div className="news-llm-search">
          <input value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder="Exact model and variant, for example model:3b" />
          <button type="button" onClick={() => pull()} disabled={Boolean(busy)}><FiDownload /> Pull model</button>
        </div>
        {catalog.length > 0 && <div className="news-llm-catalog">
          {catalog.map((model) => <article key={model.name} className="news-llm-catalog-item">
            <header>
              <div>
                <strong>{model.name}</strong>
                <span>{model.description}</span>
              </div>
              {(model.variants || []).length === 0
                ? <button type="button" onClick={() => pull(model.name)} disabled={Boolean(busy)}><FiDownload /> Pull</button>
                : <span className="news-llm-choose-variant">Choose a variant</span>}
            </header>
            <div className="news-llm-catalog-facts">
              <span><b>Capabilities</b>{(model.capabilities || []).join(', ') || 'Text'}</span>
              <span><b>Parameters</b>{(model.parameter_sizes || []).join(', ') || 'Not listed'}</span>
              <span><b>Pulls</b>{model.pulls || 'Not listed'}</span>
              <span><b>Tags</b>{model.tag_count ?? 'Not listed'}</span>
              <span><b>Updated</b>{model.updated_at || model.updated || 'Not listed'}</span>
            </div>
            {model.requirements && <p className="news-llm-requirement">{model.requirements}</p>}
            {(model.variants || []).length > 0 && <details>
              <summary>{model.variants.length} available variants</summary>
              <div className="news-llm-variants">
                <div className="news-llm-variant-heading"><span>Model</span><span>Download</span><span>Context</span><span>Input</span><span>Updated</span><span>Install</span></div>
                {model.variants.map((variant) => <div key={variant.name}>
                  <strong>{variant.name}{variant.distribution !== 'local' ? ` · ${variant.distribution}` : ''}</strong>
                  <span>{variant.size || 'N/A'}</span>
                  <span>{variant.context_window || 'N/A'}</span>
                  <span>{variant.input || 'N/A'}</span>
                  <span>{variant.updated || 'N/A'}</span>
                  <button
                    type="button"
                    onClick={() => pull(variant.name)}
                    disabled={Boolean(busy) || variant.local_compatible === false}
                    aria-label={`Pull ${variant.name}`}
                    title={variant.local_compatible === false ? 'This variant is not compatible with the local NVIDIA container' : `Pull ${variant.name}`}
                  ><FiDownload /></button>
                </div>)}
              </div>
            </details>}
            <a href={model.url} target="_blank" rel="noreferrer">Full model page and benchmarks <FiExternalLink /></a>
          </article>)}
        </div>}
      </section>
    </section>
  );
}
