from fastapi.responses import JSONResponse

from routers.frontend_api import _portfolio_contract, _unwrap_response


def test_unwrap_response_decodes_json_response():
    payload = {"account": {"equity": 1000}}

    assert _unwrap_response(JSONResponse(payload)) == payload


def test_portfolio_contract_flattens_dashboard_fields():
    portfolio_payload = {
        "account": {"equity": 1000, "cash": 200, "day_pl": 10},
        "account_meta": {"accountNumber": "123"},
        "capabilities": {"trading": True},
        "positions": [{"symbol": "AAPL"}],
        "fills": [{"symbol": "AAPL", "side": "SELL"}],
        "price_buckets": [{"bucket": "$100+"}],
        "recent_trades": [{"symbol": "AAPL", "pl": 25}],
    }

    contract = _portfolio_contract(portfolio_payload)

    assert contract["portfolio"] == portfolio_payload
    assert contract["account"] == portfolio_payload["account"]
    assert contract["account_meta"] == portfolio_payload["account_meta"]
    assert contract["capabilities"] == portfolio_payload["capabilities"]
    assert contract["positions"] == portfolio_payload["positions"]
    assert contract["fills"] == portfolio_payload["fills"]
    assert contract["price_buckets"] == portfolio_payload["price_buckets"]
    assert contract["recent_trades"] == portfolio_payload["recent_trades"]


def test_portfolio_contract_defaults_missing_sections():
    contract = _portfolio_contract(None)

    assert contract["portfolio"] == {}
    assert contract["account"] == {}
    assert contract["account_meta"] == {}
    assert contract["capabilities"] == {}
    assert contract["positions"] == []
    assert contract["fills"] == []
    assert contract["price_buckets"] == []
    assert contract["recent_trades"] == []
