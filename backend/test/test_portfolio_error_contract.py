from routers.portfolio import _schwab_error_payload, _schwab_error_status


class StatusError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_schwab_error_status_preserves_auth_and_rate_limit_codes():
    assert _schwab_error_status(StatusError("unauthorized", 401)) == 401
    assert _schwab_error_status(StatusError("forbidden", 403)) == 403
    assert _schwab_error_status(StatusError("too many requests", 429)) == 429


def test_schwab_error_status_infers_common_messages():
    assert _schwab_error_status(Exception("access token expired")) == 401
    assert _schwab_error_status(Exception("rate limit exceeded")) == 429
    assert _schwab_error_status(Exception("upstream unavailable")) == 502


def test_schwab_error_payload_is_structured():
    payload = _schwab_error_payload(Exception("upstream unavailable"), "portfolio_summary")

    assert payload == {
        "error": "schwab_api_error",
        "resource": "portfolio_summary",
        "message": "upstream unavailable",
    }
