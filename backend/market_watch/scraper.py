import requests
from bs4 import BeautifulSoup


class MarketWatchScraper:
    BASE_URL = "https://www.marketwatch.com/investing/stock/{}"

    def __init__(self):
        self.soup = None

    def fetch(self, symbol):
        """Fetch and parse MarketWatch page for a given stock symbol."""
        symbol = symbol.upper()
        url = self.BASE_URL.format(symbol)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.google.com",
            "DNT": "1",  # Do Not Track
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }

        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            self.soup = BeautifulSoup(response.text, "lxml")
        else:
            raise Exception(f"Failed to fetch {symbol}: {response.status_code}")

    def _extract_stat_from_list(self, label):
        """Extract a statistic from the <ul class='list--kv'> by label."""
        if not self.soup:
            raise ValueError("Soup not initialized. Call fetch(symbol) first.")

        ul_lists = self.soup.select("ul.list--kv")
        for ul in ul_lists:
            for li in ul.find_all("li", class_="kv__item"):
                key = li.find("small", class_="label")
                value = li.find("span", class_="primary")
                if key and value and label.lower() in key.text.lower():
                    return value.text.strip()
        return None

    def get_stats(self):
        """Returns a dictionary of key stats from the currently loaded page."""
        return {
            "Public Float": self._extract_stat_from_list("Public Float"),
            "Shares Outstanding": self._extract_stat_from_list("Shares Outstanding"),
            "Market Cap": self._extract_stat_from_list("Market Cap"),
            "Beta": self._extract_stat_from_list("Beta"),
            "P/E Ratio": self._extract_stat_from_list("P/E Ratio"),
            "EPS": self._extract_stat_from_list("EPS"),
            "Dividend Yield": self._extract_stat_from_list("Dividend Yield"),
            "52 Week Range": self._extract_stat_from_list("52 Week Range"),
            "Average Volume": self._extract_stat_from_list("Average Volume"),
        }


# scraper = MarketWatchScraper()
# scraper.fetch("AGRI")  # Load data for Apple
# apple_stats = scraper.get_stats()

# scraper.fetch("GOOGL")  # Now load Alphabet
# googl_stats = scraper.get_stats()

# print("AAPL:", apple_stats)
# print("GOOGL:", googl_stats)
