from types import SimpleNamespace

import main


class RequestStub:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.state = SimpleNamespace()


def test_request_id_uses_incoming_header():
    request = RequestStub(headers={"X-Request-ID": "request-123"})

    assert main._request_id(request) == "request-123"
    assert request.state.request_id == "request-123"


def test_request_id_is_stable_when_generated():
    request = RequestStub()

    first = main._request_id(request)
    second = main._request_id(request)

    assert first
    assert first == second
