const MANUAL_LOGOUT_KEY = 'stockapp_manual_logout';
const AUTO_TRADER_RETAINED_KEY = 'stockapp_logout_auto_trader_retained';

export function markManualLogout(autoTraderRetained) {
  localStorage.setItem(MANUAL_LOGOUT_KEY, '1');
  localStorage.setItem(AUTO_TRADER_RETAINED_KEY, autoTraderRetained ? '1' : '0');
}

export function clearManualLogout() {
  localStorage.removeItem(MANUAL_LOGOUT_KEY);
  localStorage.removeItem(AUTO_TRADER_RETAINED_KEY);
}

export function manualLogoutState() {
  return {
    active: localStorage.getItem(MANUAL_LOGOUT_KEY) === '1',
    autoTraderRetained: localStorage.getItem(AUTO_TRADER_RETAINED_KEY) === '1',
  };
}
