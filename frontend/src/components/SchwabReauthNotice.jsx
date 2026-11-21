import { FiAlertTriangle, FiRefreshCw } from 'react-icons/fi';
import { useLocation } from 'react-router-dom';
import { useAuth } from '../auth/authState';

function remainingLabel(seconds) {
  if (seconds <= 0) return 'Authorization has expired.';
  const hours = Math.max(1, Math.ceil(seconds / 3600));
  return `Authorization expires in ${hours} ${hours === 1 ? 'hour' : 'hours'}.`;
}

export default function SchwabReauthNotice() {
  const { authStatus, login } = useAuth();
  const location = useLocation();
  if (!authStatus?.reauthorization_soon) return null;

  const returnPath = `${location.pathname}${location.search}${location.hash}` || '/dashboard';
  return (
    <div className="schwab-reauth-notice" role="alert">
      <FiAlertTriangle aria-hidden="true" />
      <div>
        <strong>Schwab reauthorization needed</strong>
        <span>{remainingLabel(authStatus.seconds_to_reauthorization)}</span>
      </div>
      <button type="button" onClick={() => login(returnPath)}>
        <FiRefreshCw aria-hidden="true" /> Reauthorize
      </button>
    </div>
  );
}
