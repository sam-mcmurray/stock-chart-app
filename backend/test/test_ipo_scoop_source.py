from types import SimpleNamespace

from news_sources import ipo_scoop_source


def _response(text="", payload=None, pages="1"):
    return SimpleNamespace(
        text=text,
        headers={"X-WP-TotalPages": pages},
        json=lambda: payload or [],
    )


def test_load_ipo_calendar_uses_expected_trade_date(monkeypatch):
    page = """
    <table class="ipolist"><thead><tr>
      <th>Company</th><th>Symbol proposed</th><th>Lead Managers</th><th>Shares</th>
      <th>Price Low</th><th>Price High</th><th>Volume</th><th>Expected to Trade</th>
    </tr></thead><tbody><tr>
      <td><a href="https://www.iposcoop.com/ipo/example/">Example Inc.</a></td>
      <td>EXMP</td><td>Bank</td><td>1</td><td>10</td><td>12</td><td>12m</td><td>7/24/2026 Friday</td>
    </tr></tbody></table>
    """
    monkeypatch.setattr(ipo_scoop_source, "_get", lambda *_args, **_kwargs: _response(text=page))

    events = ipo_scoop_source.load_ipo_calendar()

    assert events[0]["date"] == "2026-07-24"
    assert events[0]["symbols"] == ["EXMP"]
    assert events[0]["details"]["date_basis"] == "expected_trade_date"


def test_load_ipo_pricings_uses_offer_date(monkeypatch):
    page = """
    <table class="ipolist"><thead><tr>
      <th>Company</th><th>Symbol</th><th>Industry</th><th>Offer Date</th><th>Shares</th>
    </tr></thead><tbody><tr>
      <td><a href="https://www.iposcoop.com/ipo/example/">Example Inc.</a></td>
      <td>EXMP</td><td>Technology</td><td>7/16/2026</td><td>1</td>
    </tr></tbody></table>
    """
    monkeypatch.setattr(ipo_scoop_source, "_get", lambda *_args, **_kwargs: _response(text=page))

    events = ipo_scoop_source.load_ipo_pricings(2026)

    assert events[0]["date"] == "2026-07-16"
    assert events[0]["details"]["date_basis"] == "offer_date"


def test_load_ipo_buzz_labels_date_as_publication_date(monkeypatch):
    monkeypatch.setattr(ipo_scoop_source, "_get", lambda *_args, **_kwargs: _response(payload=[{
        "id": 18705,
        "date": "2006-02-13T00:00:00",
        "link": "https://www.iposcoop.com/the-ipo-buzz/",
        "title": {"rendered": "The IPO Buzz: Example (EXMP Proposed)"},
    }], pages="17"))

    events, pages = ipo_scoop_source.load_ipo_buzz_page()

    assert pages == 17
    assert events[0]["date"] == "2006-02-13"
    assert events[0]["symbols"] == ["EXMP"]
    assert events[0]["details"]["date_basis"] == "article_publication_date"
