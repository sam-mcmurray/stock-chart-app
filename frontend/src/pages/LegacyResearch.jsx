import { useCallback, useEffect, useRef, useState } from 'react';
import { FiRefreshCw } from 'react-icons/fi';
import TrainingDataStatus from '../components/TrainingDataStatus';
import {
  getHistoricalOutcomeReport,
  getHistoricalOutcomeStatus,
  getHistoricalPriceStatus,
  getResearchExperiments,
  getLiveResearchReport,
  getLiveResearchStatus,
  getShadowEntryCandidates,
  getShadowEntrySummary,
} from '../lib/api';

const number = (value, digits = 2) => Number.isFinite(Number(value))
  ? Number(value).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
  : 'N/A';
const pct = (value) => Number.isFinite(Number(value)) ? `${number(value, 3)}%` : 'N/A';
const dateTime = (value) => value ? new Date(Number(value) * 1000).toLocaleString() : 'N/A';
const interval = (value) => Array.isArray(value) && value.length === 2
  ? `${pct(value[0])} to ${pct(value[1])}` : 'N/A';

function Metric({ label, value, tone = '' }) {
  return <div className={`research-metric ${tone}`}><span>{label}</span><strong>{value}</strong></div>;
}

function MetricCells({ row }) {
  return <>
    <td>{Number(row?.count || 0).toLocaleString()}</td>
    <td className={Number(row?.mean_return_pct) >= 0 ? 'research-positive' : 'research-negative'}>{pct(row?.mean_return_pct)}</td>
    <td>{pct(row?.win_rate_pct)}</td>
    <td className={Number(row?.mean_excess_return_pct) >= 0 ? 'research-positive' : 'research-negative'}>{pct(row?.mean_excess_return_pct)}</td>
    <td>{interval(row?.mean_excess_return_ci_95_pct)}</td>
  </>;
}

export default function LegacyResearch() {
  const [horizon, setHorizon] = useState(60);
  const [folds, setFolds] = useState(4);
  const [featureVersion, setFeatureVersion] = useState('news-hype-v1');
  const [generatorVersion, setGeneratorVersion] = useState('news-hype-candidate-generator-v1');
  const [tab, setTab] = useState(() => new URLSearchParams(window.location.search).get('tab') === 'training-data' ? 'training-data' : 'overview');
  const [breakdown, setBreakdown] = useState('session');
  const [report, setReport] = useState(null);
  const [outcomes, setOutcomes] = useState(null);
  const [prices, setPrices] = useState(null);
  const [experiments, setExperiments] = useState([]);
  const [liveResearch, setLiveResearch] = useState(null);
  const [liveReport, setLiveReport] = useState(null);
  const [shadowSummary, setShadowSummary] = useState(null);
  const [shadowCandidates, setShadowCandidates] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const loadVersion = useRef(0);
  const [pendingReport, setPendingReport] = useState(false);
  const [pendingPrices, setPendingPrices] = useState(false);
  const receiveReport = useCallback(data => {
    const pending = ['building', 'retrying'].includes(data?.status);
    setPendingReport(pending);
    if (!pending) setReport(data);
  }, []);
  const receivePrices = useCallback(data => {
    const pending = ['building', 'retrying'].includes(data?.status);
    setPendingPrices(pending);
    if (!pending) setPrices(data);
  }, []);

  const load = useCallback(async () => {
    const version = ++loadVersion.current;
    setLoading(true);
    setError('');
    setReport(null);
    const requests = [
      ['Historical report', () => getHistoricalOutcomeReport(horizon, folds, featureVersion, generatorVersion), receiveReport],
      ['Outcome status', getHistoricalOutcomeStatus, setOutcomes],
      ['Price coverage', getHistoricalPriceStatus, receivePrices],
      ['Experiments', getResearchExperiments, setExperiments],
      ['Live status', getLiveResearchStatus, setLiveResearch],
      ['Live report', getLiveResearchReport, setLiveReport],
      ['Shadow summary', getShadowEntrySummary, setShadowSummary],
      ['Shadow candidates', getShadowEntryCandidates, setShadowCandidates],
    ];
    await Promise.all(requests.map(async ([label, fetchData, setData]) => {
      try {
        const data = await fetchData();
        if (version === loadVersion.current) setData(data);
      } catch (err) {
        if (version === loadVersion.current) setError(previous =>
          `${previous ? `${previous} · ` : ''}${label}: ${err?.message || 'Unavailable'}`);
      }
    }));
    if (version === loadVersion.current) setLoading(false);
  }, [horizon, folds, featureVersion, generatorVersion, receiveReport, receivePrices]);

  useEffect(() => { load(); return () => { loadVersion.current += 1; }; }, [load]);

  useEffect(() => {
    if (!pendingReport && !pendingPrices) return undefined;
    let active = true;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        await Promise.all([
          ...(pendingReport ? [getHistoricalOutcomeReport(horizon, folds, featureVersion, generatorVersion)
            .then(data => { if (active) receiveReport(data); })] : []),
          ...(pendingPrices ? [getHistoricalPriceStatus()
            .then(data => { if (active) receivePrices(data); })] : []),
        ]);
      } catch (err) {
        if (active) setError(err?.message || 'Research refresh unavailable');
      } finally { inFlight = false; }
    };
    const timer = window.setInterval(poll, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [pendingReport, pendingPrices, horizon, folds, featureVersion, generatorVersion, receiveReport, receivePrices]);

  useEffect(() => {
    if (tab !== 'entry-reevaluation') return undefined;
    let active = true;
    let inFlight = false;
    const refreshShadow = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const [nextSummary, nextCandidates] = await Promise.all([
          getShadowEntrySummary(),
          getShadowEntryCandidates(),
        ]);
        if (active) {
          setShadowSummary(nextSummary);
          setShadowCandidates(nextCandidates);
        }
      } catch {
        // Keep the last successful snapshot visible during a transient poll failure.
      } finally {
        inFlight = false;
      }
    };
    refreshShadow();
    const timer = window.setInterval(refreshShadow, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [tab]);

  const overall = report?.overall || {};
  const hypothesisCohort = report?.hypothesis_cohort;
  const experiment = experiments.find((row) => (
    row.feature_version === (report?.feature_version || featureVersion)
    && row.generator_version === (report?.generator_version || generatorVersion)
  ));
  const breakdownRows = {
    session: report?.by_session || [],
    symbol: report?.by_symbol || [],
    catalyst: report?.by_catalyst || [],
    source_record_type: report?.by_source_record_type || [],
  }[breakdown];

  return <div className="research-page">
    <div className="research-header">
      <div>
        <h2 className="page-title">{tab === 'training-data' ? 'Training Data' : 'Outcome Research'}</h2>
        <div className="research-boundary">{tab === 'training-data' ? 'Historical dataset preparation' : 'Retrospective · ranking only · live activation disabled'}</div>
      </div>
      {tab !== 'training-data' && <div className="research-controls">
        <label>Version
          <select
            value={`${generatorVersion}|${featureVersion}`}
            onChange={(event) => {
              const [generator, feature] = event.target.value.split('|');
              setGeneratorVersion(generator);
              setFeatureVersion(feature);
            }}
          >
            {(report?.available_versions || [{ generator_version: generatorVersion, feature_version: featureVersion }]).map((row) => (
              <option key={`${row.generator_version}|${row.feature_version}`} value={`${row.generator_version}|${row.feature_version}`}>
                {row.feature_version}
              </option>
            ))}
          </select>
        </label>
        <label>Horizon
          <select value={horizon} onChange={(event) => setHorizon(Number(event.target.value))}>
            <option value={5}>5 bars</option><option value={15}>15 bars</option>
            <option value={60}>60 bars</option><option value={390}>390 bars</option>
          </select>
        </label>
        <label>Folds
          <select value={folds} onChange={(event) => setFolds(Number(event.target.value))}>
            {[3, 4, 5, 6].map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
        </label>
        <button type="button" className="research-refresh" onClick={load} disabled={loading} title="Refresh research results" aria-label="Refresh research results"><FiRefreshCw /></button>
      </div>}
    </div>

    {tab !== 'training-data' && <>
      {error && <div className="state-error" role="alert">{error}</div>}
      {loading && !report && <div className="panel">Loading research results...</div>}
      {pendingReport && <div role="status">Historical snapshot building...</div>}
      {pendingPrices && <div role="status">Price coverage snapshot building...</div>}
      {report?.snapshot && <div className="research-footnote">Historical snapshot: {dateTime(report.snapshot.generated_ts)}{report.snapshot.stale ? ' · Refresh pending' : ''}{report.snapshot.refresh_failed ? ' · Last refresh failed' : ''}</div>}
      {prices?.snapshot && <div className="research-footnote">Price coverage snapshot: {dateTime(prices.snapshot.generated_ts)}{prices.snapshot.stale ? ' · Refresh pending' : ''}</div>}
    </>}
    <>
      {tab !== 'training-data' && experiment && <section className="research-experiment" aria-label="Preregistered experiment">
        <div><strong>Preregistered experiment</strong><span>{experiment.experiment_version}</span></div>
        <p>{experiment.hypothesis}</p>
        <div className="research-experiment-meta">
          <span>Registered {dateTime(experiment.registered_at)}</span>
          <span>Primary: {experiment.primary_metric}</span>
          <span>{experiment.horizon_bars} bars</span>
          <span>{Number(experiment.minimum_post_registration_live_received_sample).toLocaleString()} live-received minimum</span>
          <span>{Number(liveResearch?.hypothesis_cohort_count || 0).toLocaleString()} independent live episodes</span>
          <span>{Number(liveResearch?.hypothesis_cohort_outcome_count || 0).toLocaleString()} complete cohort outcomes</span>
          <span>{Number(liveResearch?.partial_outcome_count || 0).toLocaleString()} collecting</span>
          <span>{Number(liveResearch?.unevaluated_count || 0).toLocaleString()} awaiting evaluation</span>
          <span>{Number(liveResearch?.insufficient_outcome_count || 0).toLocaleString()} missed entry window</span>
          <span>{number(liveResearch?.sample_progress_pct, 1)}% sample progress</span>
          {liveResearch?.collection_scheduler && <>
            <span>{liveResearch.collection_scheduler.version} since {dateTime(liveResearch.collection_scheduler.started_at)}</span>
            <span>{Number(liveResearch.collection_scheduler.candidate_count || 0).toLocaleString()} post-fix candidates</span>
            <span>{Number(liveResearch.collection_scheduler.collecting_count || 0).toLocaleString()} post-fix collecting</span>
            <span>{Number(liveResearch.collection_scheduler.complete_count || 0).toLocaleString()} post-fix complete</span>
            <span>{Number(liveResearch.collection_scheduler.insufficient_count || 0).toLocaleString()} post-fix missed</span>
          </>}
        </div>
        <div className="research-metrics" aria-label="Live experiment readout">
          <Metric label="Live readout" value={liveReport?.formal_readout_available ? 'Sample ready' : 'Accumulating'} />
          <Metric label="Cohort outcomes" value={Number(liveReport?.hypothesis_cohort?.count || 0).toLocaleString()} />
          <Metric label="Cohort mean MFE" value={pct(liveReport?.hypothesis_cohort?.mean_mfe_pct)} />
          <Metric label={`Cohort excess vs ${liveReport?.benchmark_symbol || 'SPY'}`} value={pct(liveReport?.hypothesis_cohort?.mean_excess_return_pct)} />
          <Metric label={`${liveReport?.benchmark_symbol || 'SPY'} coverage`} value={`${Number(liveReport?.hypothesis_cohort?.benchmark_coverage_count || 0).toLocaleString()} / ${Number(liveReport?.hypothesis_cohort?.count || 0).toLocaleString()}`} />
          <Metric label="Top attention MFE" value={pct(liveReport?.top_attention_quartile?.mean_mfe_pct)} />
          <Metric label="Top vs cohort MFE" value={pct(liveReport?.top_vs_cohort_mean_mfe_difference_pct)} />
          <Metric label="Activation" value="Disabled" />
        </div>
      </section>}
      {tab !== 'training-data' && report && <div className="research-metrics">
        <Metric label="Usable outcomes" value={Number(report.usable_outcome_count || 0).toLocaleString()} />
        <Metric label="Mean return" value={pct(overall.mean_return_pct)} tone={Number(overall.mean_return_pct) >= 0 ? 'research-positive' : 'research-negative'} />
        <Metric label={`${report.benchmark_symbol || 'SPY'} return`} value={pct(overall.mean_benchmark_return_pct)} />
        <Metric label="Excess return" value={pct(overall.mean_excess_return_pct)} tone={Number(overall.mean_excess_return_pct) >= 0 ? 'research-positive' : 'research-negative'} />
        <Metric label="Win rate" value={pct(overall.win_rate_pct)} />
        <Metric label={prices?.coverage_status === 'not_requested' ? 'Candles in import tasks' : 'Price candles'} value={prices?.coverage_status === 'not_requested' ? Number(prices.task_candles || 0).toLocaleString() : prices?.coverage?.count == null ? 'N/A' : Number(prices.coverage.count).toLocaleString()} />
      </div>}

      <div className="research-tabs" role="tablist" aria-label="Research views">
        {['overview', 'training-data', 'entry-reevaluation', 'walk-forward', 'breakdowns'].map((value) => <button type="button" role="tab" aria-selected={tab === value} className={tab === value ? 'active' : ''} onClick={() => setTab(value)} key={value}>{value.replace('-', ' ')}</button>)}
      </div>

      {tab === 'training-data' && <TrainingDataStatus />}
      {report && tab === 'overview' && <section className="panel research-section">
        <div className="research-section-header"><h3>Score Ranks</h3><span>{report.feature_version} · {report.ranking_score_field || 'long_hype_score'}</span></div>
        <div className="table-scroll"><table className="research-table"><thead><tr><th>Rank</th><th>Score range</th><th>Count</th><th>Mean return</th><th>Win rate</th><th>Excess vs {report.benchmark_symbol}</th><th>Excess 95% CI</th></tr></thead>
          <tbody>{(report.descriptive_rank_buckets || []).map((row) => <tr key={row.bucket}><th>{row.bucket.replaceAll('_', ' ')}</th><td>{number(row.minimum_hype_score, 3)}–{number(row.maximum_hype_score, 3)}</td><MetricCells row={row} /></tr>)}</tbody>
        </table></div>
        <div className="research-footnote">Outcomes: complete {Number(outcomes?.outcomes?.complete || 0).toLocaleString()} · partial {Number(outcomes?.outcomes?.partial || 0).toLocaleString()} · insufficient {Number(outcomes?.outcomes?.insufficient || 0).toLocaleString()} · pending {Number(outcomes?.ready_pending || 0).toLocaleString()}</div>
        {hypothesisCohort && <div className="research-subsection">
          <div className="research-section-header"><h3>Zero-Direction Watch Cohort</h3><span>{hypothesisCohort.definition}</span></div>
          <div className="table-scroll"><table className="research-table"><thead><tr><th>Rank</th><th>Score range</th><th>Count</th><th>Mean MFE</th><th>MFE 95% CI</th><th>Mean return</th><th>Excess vs {report.benchmark_symbol}</th></tr></thead>
            <tbody>{(hypothesisCohort.descriptive_rank_buckets || []).map((row) => <tr key={row.bucket}><th>{row.bucket.replaceAll('_', ' ')}</th><td>{number(row.minimum_hype_score, 3)}–{number(row.maximum_hype_score, 3)}</td><td>{Number(row.count || 0).toLocaleString()}</td><td>{pct(row.mean_mfe_pct)}</td><td>{interval(row.mean_mfe_ci_95_pct)}</td><td>{pct(row.mean_return_pct)}</td><td>{pct(row.mean_excess_return_pct)}</td></tr>)}</tbody>
          </table></div>
        </div>}
      </section>}

      {tab === 'entry-reevaluation' && !shadowSummary && <div role="status">Shadow results pending or unavailable.</div>}
      {shadowSummary && tab === 'entry-reevaluation' && <section className="panel research-section">
        <div className="research-section-header"><h3>Shadow Entry Reevaluation</h3><span>Local simulation only · broker calls disabled</span></div>
        <div className="research-metrics">
          <Metric label="Watching" value={Number((shadowSummary?.opportunities?.watching || 0) + (shadowSummary?.opportunities?.monitoring || 0)).toLocaleString()} />
          <Metric label="Opportunity checks" value={Number(shadowSummary?.opportunity_checks || 0).toLocaleString()} />
          <Metric label="Entered" value={Number(shadowSummary?.opportunities?.entered || 0).toLocaleString()} />
          <Metric label="Expired" value={Number(shadowSummary?.opportunities?.expired || 0).toLocaleString()} />
          <Metric label="Open positions" value={Number((shadowSummary?.positions?.open || 0) + (shadowSummary?.positions?.runner || 0)).toLocaleString()} />
          <Metric label="Activation" value="Disabled" />
        </div>
        <div className="table-scroll"><table className="research-table"><thead><tr><th>Symbol</th><th>State</th><th>Latest reason</th><th>Checks</th><th>Signal</th><th>Last check</th><th>Monitor expires</th></tr></thead>
          <tbody>{shadowCandidates.map((row) => <tr key={row.candidate_id}><th>{row.symbol}</th><td>{row.status}</td><td>{String(row.reason || 'N/A').replaceAll('_', ' ')}</td><td>{Number(row.check_count || 0).toLocaleString()}</td><td>{dateTime(row.signal_ts)}</td><td>{row.evaluated_at ? new Date(row.evaluated_at).toLocaleString() : 'N/A'}</td><td>{['watching', 'monitoring'].includes(row.status) ? dateTime(row.expires_ts) : 'Final'}</td></tr>)}</tbody>
        </table></div>
        {!shadowCandidates.length && <div className="research-footnote">No evidence-backed shadow opportunities have been observed yet.</div>}
      </section>}

      {report && tab === 'walk-forward' && <section className="panel research-section">
        <div className="research-section-header"><h3>Walk-Forward Tests</h3><span>{report.selection_policy}</span></div>
        <div className="table-scroll"><table className="research-table"><thead><tr><th>Fold</th><th>Test period</th><th>Threshold</th><th>Selected</th><th>Mean return</th><th>Win rate</th><th>Excess vs {report.benchmark_symbol}</th><th>Excess 95% CI</th></tr></thead>
          <tbody>{(report.walk_forward || []).map((row) => <tr key={row.fold}><th>{row.fold}</th><td>{dateTime(row.test_start_ts)}<br />{dateTime(row.test_end_ts)}</td><td>{number(row.training_top_quartile_threshold, 3)}</td><MetricCells row={row.test_selected} /></tr>)}</tbody>
        </table></div>
      </section>}

      {report && tab === 'breakdowns' && <section className="panel research-section">
        <div className="research-section-header"><h3>Breakdowns</h3><div className="research-breakdown-tabs">{[
          ['session', 'Session'], ['symbol', 'Symbol'], ['catalyst', 'Catalyst'], ['source_record_type', 'Source'],
        ].map(([value, label]) => <button type="button" className={breakdown === value ? 'active' : ''} onClick={() => setBreakdown(value)} key={value}>{label}</button>)}</div></div>
        <div className="table-scroll"><table className="research-table"><thead><tr><th>{breakdown.replaceAll('_', ' ')}</th><th>Count</th><th>Mean return</th><th>Win rate</th><th>Excess vs {report.benchmark_symbol}</th><th>Excess 95% CI</th></tr></thead>
          <tbody>{breakdownRows.map((row) => <tr key={row[breakdown]}><th>{String(row[breakdown]).replaceAll('_', ' ')}</th><MetricCells row={row} /></tr>)}</tbody>
        </table></div>
      </section>}
    </>
  </div>;
}
