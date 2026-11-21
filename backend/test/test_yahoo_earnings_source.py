from types import SimpleNamespace

import pytest

from news_sources import yahoo_earnings_source


PAGE = """
<section data-testid="calendar-event-table">
  <div data-testid="cal-info"><p>Earnings On Thu, Nov 6</p></div>
  <table>
    <thead><tr>
      <th>Symbol</th><th>Company</th><th>Event Name</th><th>Earnings Call Time</th>
      <th>EPS Estimate</th><th>Reported EPS</th><th>Surprise (%)</th>
      <th>Market Cap</th><th>Follow</th>
    </tr></thead>
    <tbody>
      <tr><td>AZN</td><td>AstraZeneca PLC</td><td>Q3 2025 Earnings Announcement</td>
      <td>BMO</td><td>1.13</td><td>1.19</td><td>+4.88</td><td>260.97B</td><td></td></tr>
      <tr><td>LOSS</td><td>Loss Corp.</td><td>-</td>
      <td>TAS</td><td>-0.25</td><td>(0.30)</td><td>-20</td><td>15.5M</td><td></td></tr>
    </tbody>
  </table>
  <span>1-25 of 513</span>
</section>
"""

EMPTY_PAGE = """
<main>
  <h2>Earnings Calendar for:</h2>
  <div>Jul 5, 2026 - Jul 11, 2026</div>
</main>
"""


def test_parse_yahoo_earnings_preserves_training_fields():
    rows, total = yahoo_earnings_source.parse_earnings_page(
        PAGE,
        "2025-11-06",
        "https://finance.yahoo.test/calendar",
    )

    assert total == 513
    assert rows[0]["symbol"] == "AZN"
    assert rows[0]["fiscal_quarter"] == 3
    assert rows[0]["fiscal_year"] == 2025
    assert rows[0]["eps_estimate"] == 1.13
    assert rows[0]["eps_actual"] == 1.19
    assert rows[0]["surprise_percent"] == 4.88
    assert rows[0]["market_cap_value"] == 260_970_000_000
    assert rows[0]["raw_payload"]["Company"] == "AstraZeneca PLC"
    assert rows[1]["eps_actual"] == -0.30
    assert rows[1]["event_name"] is None


def test_yahoo_page_rejects_silently_wrong_day():
    with pytest.raises(yahoo_earnings_source.YahooEarningsError, match="requested day"):
        yahoo_earnings_source.parse_earnings_page(
            PAGE.replace("Thu, Nov 6", "Fri, Nov 7"),
            "2025-11-06",
            "https://finance.yahoo.test/calendar",
        )


def test_yahoo_page_accepts_matching_empty_week_without_a_table():
    rows, total = yahoo_earnings_source.parse_earnings_page(
        EMPTY_PAGE,
        "2026-07-06",
        "https://finance.yahoo.test/calendar",
    )

    assert rows == []
    assert total == 0


def test_yahoo_page_rejects_empty_page_for_wrong_week():
    with pytest.raises(yahoo_earnings_source.YahooEarningsError, match="requested day"):
        yahoo_earnings_source.parse_earnings_page(
            EMPTY_PAGE,
            "2026-07-13",
            "https://finance.yahoo.test/calendar",
        )


def test_yahoo_url_uses_week_bounds_and_hundred_row_pages():
    url = yahoo_earnings_source.earnings_url("2025-11-06", offset=100, size=100)

    assert "from=2025-11-02" in url
    assert "to=2025-11-08" in url
    assert "day=2025-11-06" in url
    assert "offset=100" in url
    assert "size=100" in url


def test_load_page_reports_rate_limit():
    response = SimpleNamespace(status_code=429, text="", headers={})

    with pytest.raises(yahoo_earnings_source.YahooEarningsRateLimited):
        yahoo_earnings_source.load_earnings_page(
            "2025-11-06",
            get=lambda *_args, **_kwargs: response,
        )


def test_calendar_projection_exposes_eps_and_timing():
    rows, _total = yahoo_earnings_source.parse_earnings_page(
        PAGE,
        "2025-11-06",
        "https://finance.yahoo.test/calendar",
    )

    event = yahoo_earnings_source.calendar_event(rows[0])

    assert event["type"] == "Earnings watch"
    assert event["symbols"] == ["AZN"]
    assert event["details"]["call_time_label"] == "before_market_open"
    assert event["details"]["eps_actual"] == 1.19
    assert event["details"]["status"] == "reported"
