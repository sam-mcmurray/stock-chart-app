# accounts.py (or wherever your class lives)
import os
import uuid
import requests
from typing import List, Dict, Any, Optional

from auth.schwab_oauth import get_valid_access_token
from schwab.user_preferences import UserPreferences

BASE_URL = "https://api.schwabapi.com/trader/v1"


class SchwabAccountClient:
    """
    Schwab account client with eager capabilities detection.
    On init, it fetches /accounts?fields=positions, selects a primary account,
    and computes capabilities (margin/options/short/closing_only).
    """

    def __init__(
        self,
        user_prefs: UserPreferences,
        access_token: Optional[str] = None,
        prefer_account_number: Optional[str] = None,
        eager_load: bool = True,
    ):
        self.access_token = (
            access_token
            or getattr(user_prefs, "access_token", None)
            or get_valid_access_token()
        )
        self.correlation_id = (user_prefs.get_streamer_credentials() or {}).get("correlation_id")
        self.prefer_account_number = prefer_account_number or os.getenv("SCHWAB_ACCOUNT_NUMBER")

        # caches
        self._accounts_payload: Optional[List[dict]] = None  # raw list of {securitiesAccount:{...}}
        self.securities_account: Optional[dict] = None       # chosen { ... } (inner object)
        self.capabilities: Optional[dict] = None

        # convenience booleans (mirrors capabilities)
        self.has_margin: bool = False
        self.options_enabled: bool = False
        self.can_short: bool = False
        self.closing_only: bool = False

        if eager_load:
            self.refresh_summary()

    # -----------------------------
    # Low-level HTTP
    # -----------------------------
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Schwab-Client-CorrelId": self.correlation_id or str(uuid.uuid4()),
        }

    def _get(self, endpoint: str, params=None):
        url = f"{BASE_URL}/{endpoint}"
        r = requests.get(url, headers=self._headers(), params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    # -----------------------------
    # Public API wrappers
    # -----------------------------
    def get_account_numbers(self):
        """GET /accounts/accountNumbers"""
        return self._get("accounts/accountNumbers")

    def get_all_accounts(self, include_positions: bool = False):
        """GET /accounts[?fields=positions]"""
        params = {"fields": "positions"} if include_positions else None
        return self._get("accounts", params)

    def get_account(self, encrypted_account_id: str, include_positions: bool = False):
        """GET /accounts/{accountId}[?fields=positions]"""
        params = {"fields": "positions"} if include_positions else None
        return self._get(f"accounts/{encrypted_account_id}", params)

    # -----------------------------
    # Eager load + capabilities
    # -----------------------------
    def refresh_summary(self) -> dict:
        """
        Fetch /accounts?fields=positions, choose primary account, and compute capabilities.
        Returns dict with 'account' and 'capabilities'.
        """
        payload = self.get_all_accounts(include_positions=True) or []
        self._accounts_payload = payload
        self.securities_account = self._choose_primary(payload)

        caps = self._infer_capabilities(self.securities_account)
        self.capabilities = caps

        # mirror as convenience flags
        self.has_margin = bool(caps.get("has_margin"))
        self.options_enabled = bool(caps.get("options_enabled"))
        self.can_short = bool(caps.get("can_short"))
        self.closing_only = bool(caps.get("closing_only"))

        return {"account": self.securities_account, "capabilities": caps}

    # -----------------------------
    # Helpers
    # -----------------------------
    def _choose_primary(self, accounts_list: List[dict]) -> Optional[dict]:
        """
        Input is a list of { 'securitiesAccount': {...} }.
        If SCHWAB_ACCOUNT_NUMBER is set, prefer that, else first.
        """
        if not accounts_list:
            return None

        if self.prefer_account_number:
            for row in accounts_list:
                sa = (row or {}).get("securitiesAccount") or {}
                if str(sa.get("accountNumber")) == str(self.prefer_account_number):
                    return sa

        return (accounts_list[0] or {}).get("securitiesAccount")

    @staticmethod
    def _f(x, default=0.0) -> float:
        try:
            return float(x)
        except Exception:
            return float(default)

    def _infer_capabilities(self, sa: Optional[dict]) -> dict:
        """
        Infer capabilities from a single securitiesAccount dict.
        - has_margin: margin-related balances or stockBuyingPower > cash
        - options_enabled: optionBuyingPower > 0
        - can_short: has_margin and not closing-only (short fields may be 0 when unused)
        - closing_only: isClosingOnlyRestricted flag
        """
        sa = sa or {}
        init = sa.get("initialBalances") or {}
        curr = sa.get("currentBalances") or {}
        proj = sa.get("projectedBalances") or {}

        f = self._f

        # Margin signals
        stock_bp = f(curr.get("stockBuyingPower"))
        cash_bal = f(init.get("cashBalance") or init.get("totalCash"))
        any_margin_fields = any([
            "marginBalance" in curr or "shortBalance" in curr,
            "marginBalance" in init or "longMarginValue" in init,
            "marginBalance" in proj,
        ])
        has_margin = any_margin_fields or (stock_bp > max(cash_bal, 0))

        # Options signal
        options_bp = f(curr.get("optionBuyingPower"))
        options_enabled = options_bp > 0

        # Short capability (requires margin; short fields may be zero if no active shorts)
        short_nonzero = any(
            f(curr.get(k)) != 0 or f(init.get(k)) != 0 or f(proj.get(k)) != 0
            for k in ("shortBalance", "shortStockValue", "shortMarginValue")
        )
        closing_only = bool(sa.get("isClosingOnlyRestricted"))
        can_short = has_margin and not closing_only and (short_nonzero or has_margin)

        return {
            "has_margin": bool(has_margin),
            "options_enabled": bool(options_enabled),
            "can_short": bool(can_short),
            "closing_only": closing_only,
            "signals": {
                "optionBuyingPower": options_bp,
                "stockBuyingPower": stock_bp,
                "cashBalance": cash_bal,
                "short_fields_nonzero": short_nonzero,
            },
        }
