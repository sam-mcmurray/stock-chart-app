import { useEffect, useState } from 'react';
import { FiRefreshCw } from 'react-icons/fi';
import { getTrainingDataStatus } from '../lib/api';

const count = value => value == null ? 'Not available' : Number(value).toLocaleString();
const label = value => value ? value.replaceAll('_', ' ') : 'Not started';

export default function TrainingDataStatus() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const result = await getTrainingDataStatus();
        if (active) { setData(result); setError(''); }
      } catch (err) {
        if (active) setError(err.message || 'Training data status unavailable');
      } finally { pending = false; }
    }
    load();
    const timer = setInterval(load, 15000);
    return () => { active = false; clearInterval(timer); };
  }, [refresh]);

  return <section className="training-data-section" aria-label="Training data preparation">
    <div className="research-header">
      <h3>Training data</h3>
      <button className="research-refresh" type="button" aria-label="Refresh training data" title="Refresh training data" onClick={() => setRefresh(value => value + 1)}><FiRefreshCw /></button>
    </div>
    {error && <p role="alert">{error} {data ? 'Last saved snapshot retained.' : ''}</p>}
    {!data && !error && <p role="status">Loading training data status...</p>}
    {data && <>
      <dl className="training-data-facts">
        <div><dt>Saved job status</dt><dd>{label(data.status)}</dd></div>
        <div><dt>Stage</dt><dd>{label(data.stage)}</dd></div>
        <div><dt>Current month</dt><dd>{data.current_month || 'Not available'}</dd></div>
        <div><dt>Target start</dt><dd>{data.target_start || 'Not available'}</dd></div>
        <div><dt>Target end (exclusive)</dt><dd>{data.target_end_exclusive || 'Not available'}</dd></div>
        <div><dt>Completed months</dt><dd>{count(data.completed_months?.length)}</dd></div>
        <div><dt>Last saved update</dt><dd>{data.snapshot_ts ? new Date(data.snapshot_ts * 1000).toLocaleString() : 'Not available'}</dd></div>
        <div><dt>Snapshot age</dt><dd>{data.snapshot_age_seconds == null ? 'Not available' : `${count(data.snapshot_age_seconds)} seconds`}</dd></div>
      </dl>
      {(data.error_type || data.snapshot_errors?.length > 0) && <p role="alert">{data.error_type || 'Unreadable progress snapshot'}</p>}
      <div className="training-data-stages">
        <section><h4>News export</h4><dl>
          <dt>Status</dt><dd>{label(data.export?.status)}</dd>
          <dt>Exported dates</dt><dd>{data.export?.first_date ? `${data.export.first_date} to ${data.export.last_date}` : 'Not available'}</dd>
          <dt>Calendar days</dt><dd>{count(data.export?.days)}</dd>
          <dt>Articles</dt><dd>{count(data.export?.articles)}</dd>
          <dt>Symbols</dt><dd>{count(data.export?.symbols)}</dd>
        </dl></section>
        <section><h4>Price acquisition</h4><dl>
          <dt>Status</dt><dd>{label(data.acquisition?.status)}</dd>
          <dt>Completed requests</dt><dd>{count(data.acquisition?.completed)}</dd>
          <dt>Remaining requests</dt><dd>{count(data.acquisition?.remaining)}</dd>
          <dt>No-data responses</dt><dd>{count(data.acquisition?.no_data)}</dd>
          <dt>Current symbol</dt><dd>{data.acquisition?.symbol || 'Not available'}</dd>
        </dl></section>
        <section><h4>Session assembly</h4><dl>
          <dt>Status</dt><dd>{label(data.assembly?.status)}</dd>
          <dt>Assembled symbol-days</dt><dd>{count(data.assembly?.completed)}</dd>
          <dt>Remaining symbol-days</dt><dd>{count(data.assembly?.remaining)}</dd>
          <dt>Float</dt><dd>Unknown unless evidenced</dd>
          <dt>Chronological split</dt><dd>Not certified</dd>
          <dt>DRL training</dt><dd>Not started by this job</dd>
        </dl></section>
      </div>
    </>}
  </section>;
}
