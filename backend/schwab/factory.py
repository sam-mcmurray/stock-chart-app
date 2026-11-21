from auth.schwab_oauth import get_valid_access_token
from schwab.user_preferences import UserPreferences
from schwab.api import SchwabAPI
from schwab.streaming_client import SchwabWebSocketClient

def create_schwab_clients():
    """
    Initializes SchwabAPI (REST) and SchwabWebSocketClient (streaming)
    using current user preferences and valid access token.

    Returns:
        (SchwabAPI, SchwabWebSocketClient)
    """
    user_prefs = UserPreferences()
    token = get_valid_access_token()

    api_client = SchwabAPI(user_prefs)
    ws_client = SchwabWebSocketClient(user_prefs, token)

    return api_client, ws_client