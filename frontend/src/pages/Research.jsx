import { useEffect, useState } from 'react';
import TrainingDataStatus from '../components/TrainingDataStatus';
import { getLiveLearningStatus } from '../lib/api';

const count = value => value == null ? 'Not available' : Number(value).toLocaleString();

function LiveLearning() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const next = await getLiveLearningStatus();
        if (active) { setData(next); setError(''); }
      } catch (err) {
        if (active) setError(err.message || 'Live status unavailable');
      } finally { pending = false; }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return <section className="training-data-section" aria-label="Live Level 2 model">
    <h3>Live Level 2</h3>
    {error && <p role="alert">{error}{data ? ' Last saved snapshot retained.' : ''}</p>}
    {!data && !error && <p role="status">Loading live status...</p>}
    {data && <>
      <dl className="training-data-facts">
        <div><dt>RL learner</dt><dd>{data.learner_status === 'not_deployed' ? 'Not deployed' : data.learner_status === 'market_closed' ? 'Market closed' : data.learner_status?.replaceAll('_', ' ')}</dd></div>
        <div><dt>Model execution</dt><dd>No live orders</dd></div>
        <div><dt>Stream capture</dt><dd>{data.capture_status || 'Unavailable'}</dd></div>
        <div><dt>Heartbeat age</dt><dd>{data.heartbeat_age_seconds == null ? 'Not available' : `${count(data.heartbeat_age_seconds)} seconds`}</dd></div>
        <div><dt>Subscribed symbols</dt><dd>{count(data.symbol_count)}</dd></div>
        <div><dt>Priority capture symbols</dt><dd>{count(data.priority_symbols)}</dd></div>
        <div><dt>Recorded observations (worker run)</dt><dd>{count(data.market_observations)}</dd></div>
        <div><dt>Capture queue</dt><dd>{count(data.market_observation_queue_depth)}</dd></div>
        <div><dt>Capture errors</dt><dd>{count(data.market_observation_errors)}</dd></div>
        <div><dt>Dropped observations</dt><dd>{count(data.market_observation_dropped)}</dd></div>
        <div><dt>Book recording interval</dt><dd>{data.book_observation_interval_ms == null ? 'Not available' : `${count(data.book_observation_interval_ms)} ms`}</dd></div>
        <div><dt>Priority recording interval</dt><dd>{data.priority_observation_interval_ms == null ? 'Not available' : `${count(data.priority_observation_interval_ms)} ms`}</dd></div>
        <div><dt>NASDAQ book age</dt><dd>{data.book_age_seconds?.NASDAQ_BOOK == null ? 'Not available' : `${count(data.book_age_seconds.NASDAQ_BOOK)} seconds`}</dd></div>
        <div><dt>NYSE book age</dt><dd>{data.book_age_seconds?.NYSE_BOOK == null ? 'Not available' : `${count(data.book_age_seconds.NYSE_BOOK)} seconds`}</dd></div>
      </dl>
      <div className="training-data-stages"><section><h4>Learning pipeline</h4><dl>
        <dt>Completed PPO updates</dt><dd>{count(data.learner?.policy_version)}</dd>
        <dt>Training transitions (worker run)</dt><dd>{count(data.learner?.process_training_transitions)}</dd>
        <dt>Active model symbols</dt><dd>{data.learner?.active_symbols?.length ?? 'Not available'}</dd>
        <dt>Learner heartbeat age</dt><dd>{data.learner?.heartbeat_age_seconds == null ? 'Not available' : `${count(data.learner.heartbeat_age_seconds)} seconds`}</dd>
        <dt>Account scope</dt><dd>{data.learner?.account_scope === 'independent_symbol_simulations' ? 'Independent symbol simulations' : 'Not available'}</dd>
        <dt>Training error</dt><dd>{data.learner?.error_type || 'None reported'}</dd>
      </dl></section></div>
    </>}
  </section>;
}

export default function Research() {
  const [tab, setTab] = useState(() => new URLSearchParams(window.location.search).get('tab') === 'training-data' ? 'minute' : 'level2');
  return <div className="research-page">
    <header className="research-header"><h2 className="page-title">RL Models</h2></header>
    <div className="research-tabs" role="tablist" aria-label="RL models">
      <button type="button" role="tab" aria-selected={tab === 'level2'} className={tab === 'level2' ? 'active' : ''} onClick={() => setTab('level2')}>Live Level 2</button>
      <button type="button" role="tab" aria-selected={tab === 'minute'} className={tab === 'minute' ? 'active' : ''} onClick={() => setTab('minute')}>1-minute model</button>
    </div>
    {tab === 'level2' ? <LiveLearning /> : <TrainingDataStatus />}
  </div>;
}
