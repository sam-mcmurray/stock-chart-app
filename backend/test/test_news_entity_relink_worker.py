from workers.news_entity_relink_worker import relink_result


def test_relink_result_removes_ambiguous_ticker_and_keeps_exact_issuers():
    resolved = {
        "Great Eastern Shipping": [],
        "ICICI Bank": ["IBN"],
        "NVIDIA": ["NVDA"],
    }
    result = relink_result({
        "related_tickers": ["GEHDF", "IBN", "NVDA"],
        "related_companies": [
            {"name": "Great Eastern Shipping", "validated_tickers": ["GEHDF"]},
            {"name": "ICICI Bank", "validated_tickers": ["IBN"]},
        ],
        "company_relationships": [{
            "company_a": "Great Eastern Shipping",
            "validated_tickers_a": ["GEHDF"],
            "company_b": "NVIDIA",
            "validated_tickers_b": ["NVDA"],
            "relationship": "partnership",
        }],
    }, resolver=lambda name: resolved.get(name, []))

    assert result["entity_link_version"] == 2
    assert result["related_tickers"] == ["IBN", "NVDA"]
    assert [company["name"] for company in result["related_companies"]] == ["ICICI Bank"]
    assert result["company_relationships"][0]["validated_tickers_a"] == []
    assert result["company_relationships"][0]["validated_tickers_b"] == ["NVDA"]


def test_relink_result_versions_tickerless_financial_article():
    result = relink_result({
        "relevance_label": "markets",
        "related_tickers": [],
        "related_companies": [],
        "company_relationships": [],
    }, resolver=lambda _name: [])

    assert result["entity_link_version"] == 2
    assert result["related_tickers"] == []
