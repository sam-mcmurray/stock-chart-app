import { useCallback, useEffect, useState } from 'react';
import { FiClock, FiLock, FiLogOut, FiRefreshCw, FiServer, FiShield, FiUser } from 'react-icons/fi';
import {
  buildLoginUrl,
  disconnectSchwab,
  getAuthStatus,
  getLocalExitReadiness,
  getNewsStatus,
  getScannerStatus,
  getStopLossStatus,
  getTradingStatus,
} from '../lib/api';
import { hardReplace } from '../lib/hardNav';

function resultValue(result) {
  return result.status === 'fulfilled' ? result.value : null;
}

function resultError(result) {
  return result.status === 'rejected' ? result.reason?.message || String(result.reason) : '';
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return 'no heartbeat';
  if (seconds < 5) return 'fresh';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function fmtExpiry(seconds) {
  if (seconds === null || seconds === undefined) return 'unknown';
  if (seconds <= 0) return 'expired';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

export default function Profile(){
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [disconnectError, setDisconnectError] = useState('');

  const loadProfile = useCallback(async () => {
    setLoading(true);
    const [auth, trading, scanner, news, stopLoss, exitReadiness] = await Promise.allSettled([
      getAuthStatus(),
      getTradingStatus(),
      getScannerStatus(),
      getNewsStatus(),
      getStopLossStatus(),
      getLocalExitReadiness(),
    ]);

    setSnapshot({
      auth: resultValue(auth),
      trading: resultValue(trading),
      scanner: resultValue(scanner),
      news: resultValue(news),
      stopLoss: resultValue(stopLoss),
      exitReadiness: resultValue(exitReadiness),
      errors: {
        auth: resultError(auth),
        trading: resultError(trading),
        scanner: resultError(scanner),
        news: resultError(news),
        stopLoss: resultError(stopLoss),
        exitReadiness: resultError(exitReadiness),
      },
    });
    setLoading(false);
  }, []);

  useEffect(() => {
    loadProfile();
  }, [loadProfile]);

  const handleDisconnect = async () => {
    setDisconnecting(true);
    setDisconnectError('');
    try {
      await disconnectSchwab();
      hardReplace(buildLoginUrl('/profile'));
    } catch (error) {
      setDisconnectError(error?.message || String(error));
      setDisconnecting(false);
    }
  };

  const authData = snapshot?.auth?.data || {};
  const errors = snapshot?.errors || {};

  return (
    <section className="page-stack">
      <div className="panel profile-header">
        <div>
          <h2 className="page-title"><FiUser /> Profile</h2>
          <p className="section-copy">Session, trading, and worker health.</p>
        </div>
        <button
          type="button"
          className="scanner-button"
          onClick={loadProfile}
          disabled={loading}
        >
          <FiRefreshCw /> {loading ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>

      {loading && !snapshot ? (
        <div className="panel">Loading profile...</div>
      ) : (
        <div className="profile-grid">
          <StatusCard
            title="Schwab Session"
            icon={<FiLock />}
            ok={Boolean(authData.authenticated)}
            rows={[
              ['Authenticated', authData.authenticated ? 'Yes' : 'No'],
              ['Token expiry', fmtExpiry(authData.seconds_to_expiry)],
              ['Reauthorization', fmtExpiry(authData.seconds_to_reauthorization)],
              ['CSRF', 'Double-submit cookie enabled'],
            ]}
            error={errors.auth}
          />

          <StatusCard
            title="Auto Trader"
            icon={<FiShield />}
            ok={Boolean(snapshot?.trading?.auto_trader_enabled)}
            statusLabel={snapshot?.trading?.auto_trader_enabled ? 'On' : 'Off'}
            rows={[
              ['Mode', snapshot?.trading?.auto_trader_enabled ? 'On' : 'Off'],
              ['Credentials', snapshot?.trading?.has_trading_credentials ? 'Stored' : 'Not stored'],
              ['Execution', snapshot?.stopLoss?.execute_trades ? 'Live orders' : 'Dry run'],
            ]}
            error={errors.trading || errors.stopLoss}
          />

          <StatusCard
            title="Scanner Worker"
            icon={<FiServer />}
            ok={Boolean(snapshot?.scanner?.ok)}
            rows={[
              ['Heartbeat', fmtAge(snapshot?.scanner?.heartbeat_age_seconds)],
              ['Focus results', snapshot?.scanner?.focus_count ?? 0],
              ['Recent tickers', snapshot?.scanner?.recent_ticker_count ?? 0],
            ]}
            error={errors.scanner}
          />

          <StatusCard
            title="Local Exit Protection"
            icon={<FiShield />}
            ok={Boolean(snapshot?.exitReadiness?.ready)}
            statusLabel={snapshot?.exitReadiness?.ready ? 'Ready' : 'Blocked'}
            rows={[
              ['Mode', snapshot?.exitReadiness?.protection_mode === 'local_trigger' ? 'Local trigger' : 'Unknown'],
              ['Market stream', fmtAge(snapshot?.exitReadiness?.account_stream_age_seconds)],
              ['Exit worker', fmtAge(snapshot?.exitReadiness?.exit_worker_age_seconds)],
              ['Backup exit worker', fmtAge(snapshot?.exitReadiness?.backup_exit_worker_age_seconds)],
              ['Journal', snapshot?.exitReadiness?.database_available ? 'Available' : 'Unavailable'],
              ['Entry block', snapshot?.exitReadiness?.reasons?.[0] || 'None'],
            ]}
            error={errors.exitReadiness}
          />

          <StatusCard
            title="News Worker"
            icon={<FiClock />}
            ok={Boolean(snapshot?.news?.ok)}
            rows={[
              ['Heartbeat', fmtAge(snapshot?.news?.heartbeat_age_seconds)],
              ['Articles', snapshot?.news?.recent_article_count ?? 0],
              ['Tickers', snapshot?.news?.recent_ticker_count ?? 0],
            ]}
            error={errors.news}
          />
        </div>
      )}

      <section className="profile-security-actions" aria-labelledby="profile-security-title">
        <div>
          <h3 id="profile-security-title">Security Actions</h3>
          <p>Remove this app's Schwab session and any stored Auto Trader credentials.</p>
        </div>
        {!confirmDisconnect ? (
          <button type="button" className="profile-disconnect-button" onClick={() => setConfirmDisconnect(true)}>
            <FiLogOut /> Disconnect Schwab
          </button>
        ) : (
          <div className="profile-disconnect-confirm" role="group" aria-label="Confirm Schwab disconnect">
            <span>You will be returned to Schwab authorization.</span>
            <button type="button" onClick={() => setConfirmDisconnect(false)} disabled={disconnecting}>Cancel</button>
            <button type="button" className="profile-disconnect-button" onClick={handleDisconnect} disabled={disconnecting}>
              <FiLogOut /> {disconnecting ? 'Disconnecting...' : 'Confirm disconnect'}
            </button>
          </div>
        )}
        {disconnectError && <div className="state-error">{disconnectError}</div>}
      </section>
    </section>
  );
}

function StatusCard({ title, icon, ok, statusLabel, rows, error }) {
  return (
    <article className="panel status-card">
      <div className="status-card-heading">
        <span className="item-icon">{icon}</span>
        <h3>{title}</h3>
        <span className={`status-pill ${ok ? 'status-pill-ok' : 'status-pill-muted'}`}>
          {statusLabel || (ok ? 'OK' : 'Check')}
        </span>
      </div>

      <dl className="status-list">
        {rows.map(([label, value]) => (
          <div key={label} className="status-row">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>

      {error && <div className="state-error status-error">{error}</div>}
    </article>
  );
}
