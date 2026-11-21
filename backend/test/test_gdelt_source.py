import os
import sys
from unittest.mock import MagicMock, patch

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from news_sources.gdelt_source import load_gdelt_articles


def test_load_gdelt_articles_maps_doc_api_articles():
    response = MagicMock()
    response.json.return_value = {
        "articles": [
            {
                "url": "https://example.com/a",
                "title": "Market headline",
                "snippet": "Market summary",
                "domain": "example.com",
                "seendate": "20260605T120000Z",
            }
        ]
    }

    with patch("news_sources.gdelt_source.requests.get", return_value=response) as mock_get:
        rows = load_gdelt_articles("market", timespan="15m", max_records=10)

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"]["sort"] == "datedesc"
    assert rows[0]["url"] == "https://example.com/a"
    assert rows[0]["headline"] == "Market headline"
    assert rows[0]["source"] == "example.com"
    assert rows[0]["datetime"] > 0
