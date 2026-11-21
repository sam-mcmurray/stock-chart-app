# data/schwab/schwab_order_client.py

import uuid
import requests
import re
from typing import Optional
from auth.schwab_oauth import get_valid_access_token
from schwab.user_preferences import UserPreferences

BASE_URL = "https://api.schwabapi.com/trader/v1"

class SchwabOrderClient:
    def __init__(self, user_prefs: UserPreferences, access_token: Optional[str] = None):
        self.access_token = access_token or getattr(user_prefs, "access_token", None) or get_valid_access_token()
        self.correlation_id = user_prefs.get_streamer_credentials().get("correlation_id")

    def _request(self, method: str, endpoint: str, params=None, json=None):
        url = f"{BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Schwab-Client-CorrelId": self.correlation_id or str(uuid.uuid4()),
            "Content-Type": "application/json"
        }
        response = requests.request(method, url, headers=headers, params=params, json=json, timeout=20)
        response.raise_for_status()
        if response.content:
            return response.json()
        location = response.headers.get("Location") or response.headers.get("location")
        order_id = None
        if location:
            match = re.search(r"/orders/(\d+)(?:$|[/?#])", location)
            order_id = int(match.group(1)) if match else None
        return {
            "status_code": response.status_code,
            "location": location,
            "order_id": order_id,
        }

    # ✅ GET /orders — All orders across all accounts
    def get_all_orders(self, from_time: str, to_time: str, max_results: int = 3000, status: str = None):
        params = {
            "fromEnteredTime": from_time,
            "toEnteredTime": to_time,
            "maxResults": max_results
        }
        if status:
            params["status"] = status
        return self._request("GET", "orders", params=params)

    # ✅ GET /accounts/{accountNumber}/orders
    def get_orders(self, account_id: str, from_time: str, to_time: str, max_results: int = 3000, status: str = None):
        params = {
            "fromEnteredTime": from_time,
            "toEnteredTime": to_time,
            "maxResults": max_results
        }
        if status:
            params["status"] = status
        return self._request("GET", f"accounts/{account_id}/orders", params=params)

    # ✅ POST /accounts/{accountNumber}/orders
    def place_order(self, account_id: str, order_payload: dict):
        return self._request("POST", f"accounts/{account_id}/orders", json=order_payload)

    # ✅ GET /accounts/{accountNumber}/orders/{orderId}
    def get_order(self, account_id: str, order_id: int):
        return self._request("GET", f"accounts/{account_id}/orders/{order_id}")

    # ✅ DELETE /accounts/{accountNumber}/orders/{orderId}
    def cancel_order(self, account_id: str, order_id: int):
        return self._request("DELETE", f"accounts/{account_id}/orders/{order_id}")

    # ✅ PUT /accounts/{accountNumber}/orders/{orderId}
    def replace_order(self, account_id: str, order_id: int, new_order_payload: dict):
        return self._request("PUT", f"accounts/{account_id}/orders/{order_id}", json=new_order_payload)

    # 🚧 POST /accounts/{accountNumber}/previewOrder — Placeholder
    def preview_order(self, account_id: str, order_payload: dict):
        raise NotImplementedError("Preview order endpoint is not available yet (Coming Soon).")
