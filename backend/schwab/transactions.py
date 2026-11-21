# data/schwab/schwab_transaction_client.py

import uuid
import requests
from auth.schwab_oauth import get_valid_access_token

BASE_URL = "https://api.schwabapi.com/trader/v1"

class SchwabTransactionClient:
    def __init__(self, user_prefs, access_token: str | None = None):
        self.access_token = (
            access_token
            or getattr(user_prefs, "access_token", None)
            or get_valid_access_token()
        )
        self.correlation_id = user_prefs.get_streamer_credentials().get("correlation_id")

    def _request(self, method: str, endpoint: str, params=None):
        url = f"{BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Schwab-Client-CorrelId": self.correlation_id or str(uuid.uuid4()),
            "Content-Type": "application/json"
        }
        response = requests.request(method, url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    # ✅ GET /accounts/{accountNumber}/transactions
    def get_transactions(self, account_id: str, start_date: str, end_date: str, types: str, symbol: str = None):
        params = {
            "startDate": start_date,
            "endDate": end_date,
            "types": types
        }
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", f"accounts/{account_id}/transactions", params=params)

    # ✅ GET /accounts/{accountNumber}/transactions/{transactionId}
    def get_transaction(self, account_id: str, transaction_id: int):
        return self._request("GET", f"accounts/{account_id}/transactions/{transaction_id}")
