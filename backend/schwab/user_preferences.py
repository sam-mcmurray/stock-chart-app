# data/schwab/user_preferences.py
import os
import requests
from auth.schwab_oauth import get_valid_access_token


class UserPreferences:
    def __init__(self, access_token: str | None = None):
        self.access_token = access_token
        raw_data = self.get_user_preferences()
        self.accounts = raw_data.get("accounts", [])
        self.streamer_info = raw_data.get("streamerInfo", [])[0] if raw_data.get("streamerInfo") else {}
        self.offers = raw_data.get("offers", [])

    def get_user_preferences(self):
        access_token = self.access_token or get_valid_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        response = requests.get(os.getenv("SCHWAB_PREFERENCES_URL"), headers=headers)
        response.raise_for_status()

        return response.json()

    def get_primary_account(self):
        for acct in self.accounts:
            if acct.get("primaryAccount"):
                return acct
        return None

    def get_streamer_credentials(self):
        if not self.streamer_info:
            raise ValueError("streamerInfo is missing from user preferences.")
        return {
            "socket_url": self.streamer_info.get("streamerSocketUrl"),
            "customer_id": self.streamer_info.get("schwabClientCustomerId"),
            "correlation_id": self.streamer_info.get("schwabClientCorrelId"),
            "channel": self.streamer_info.get("schwabClientChannel"),
            "function_id": self.streamer_info.get("schwabClientFunctionId"),
        }

    def get_offer_permissions(self):
        return self.offers[0] if self.offers else {}
