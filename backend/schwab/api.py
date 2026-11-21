from schwab.market_data import SchwabMarketDataClient
from schwab.accounts import SchwabAccountClient
from schwab.orders import SchwabOrderClient
from schwab.transactions import SchwabTransactionClient
from schwab.user_preferences import UserPreferences

class SchwabAPI:
    def __init__(self, user_prefs: UserPreferences):
        self.market = SchwabMarketDataClient(user_prefs)
        self.accounts = SchwabAccountClient(user_prefs)
        self.orders = SchwabOrderClient(user_prefs)
        self.transactions = SchwabTransactionClient(user_prefs)