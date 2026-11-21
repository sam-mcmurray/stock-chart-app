from copy import deepcopy

import pytest

from drl_agent.sec_filing_float import cache_filing, extract_float
from drl_agent.sec_share_history import build_history
from drl_agent.sec_float_evidence import build_evidence
from test.test_sec_share_history import archive
from test.test_synthetic_execution import resign


def filing(html=None):
    result = {"version": "sec-filing-cache-v1", "cik": 1800,
              "accession": "0000001800-24-000001", "url": "https://www.sec.gov/fixture.htm",
              "html": html or '''<html><body>
<ix:nonNumeric name="dei:EntityCentralIndexKey">0000001800</ix:nonNumeric>
<ix:nonNumeric name="dei:TradingSymbol">TEST</ix:nonNumeric>
<ix:nonNumeric name="dei:Security12bTitle">Common Shares, Without Par Value</ix:nonNumeric>
<p>The aggregate market value of the <span>100,000</span> shares of voting stock
held by nonaffiliates of the registrant, computed by reference to the closing
price on June 30, 2024, was $1,000,000. Test has no non-voting common equity.
Number of common shares outstanding as of July 31, 2024: 120,000</p></body></html>'''}
    resign(result, "archive_sha256")
    return result


def test_explicit_count_is_not_outstanding_and_retains_availability():
    history, source = build_history(archive()), filing()
    before = deepcopy((history, source))
    result = extract_float(history, source)
    assert result["abstentions"] == []
    row, = result["records"]
    assert row["value"] == 100000
    assert row["basis"] == "reported_nonaffiliate_shares"
    assert row["effective_date"] == "2024-06-30"
    assert row["available_ts"] == history["records"][0]["available_ts"]
    assert not result["training_ready"]
    assert (history, source) == before


@pytest.mark.parametrize("old,new,reason", [
    ("June 30, 2024", "June 29, 2024", "measurement_date_or_dollar_value_mismatch"),
    ("$1,000,000", "$2,000,000", "measurement_date_or_dollar_value_mismatch"),
    ("held by nonaffiliates", "outstanding", "explicit_nonaffiliate_count_not_found"),
    ("has no non-voting", "has non-voting", "additional_equity_scope_unverified"),
    ("0000001800</ix", "0000001801</ix", "filing_cik_unverified"),
    ("Common Shares, Without Par Value", "Preferred Shares", "common_equity_class_unverified"),
    ("</body>", '<ix:nonNumeric name="dei:TradingSymbol">OTHER</ix:nonNumeric></body>', "single_equity_class_unverified"),
])
def test_abstains_instead_of_guessing(old, new, reason):
    source = filing(filing()["html"].replace(old, new))
    result = extract_float(build_history(archive()), source)
    assert result["records"] == []
    assert reason in result["abstentions"]


def test_tampered_source_refused():
    source = filing()
    source["html"] += "tampered"
    with pytest.raises(ValueError):
        extract_float(build_history(archive()), source)


@pytest.mark.parametrize("price,date,usd,accepted", [
    ("10.00", "June 30, 2024", "1,000,000.00", True),
    ("0", "June 30, 2024", "1,000,000.00", False),
    ("10", "June 29, 2024", "1,000,000.00", False),
    ("10", "June 30, 2024", "2,000,000.00", False),
])
def test_same_cover_price_estimate_is_separate_from_reported_float(price, date, usd, accepted):
    source = filing()
    head = source["html"].split("<p>")[0]
    text = ("The aggregate market value of the registrant's common stock outstanding, "
            "other than securities held by persons who may be deemed affiliates of the registrant, "
            f"computed by reference to the closing sales price of ${price} on {date}, "
            f"as reported on NYSE, was approximately $ {usd} .")
    source = filing(head + "<p>" + text + "</p></body></html>")
    result = extract_float(build_history(archive()), source)
    assert result["records"] == []
    assert bool(result["estimates"]) is accepted
    if accepted:
        estimate, = result["estimates"]
        assert estimate["value"] == 100000
        assert not estimate["entry_eligible"]
        assert not estimate["reported_not_estimated"]
        assert build_evidence([result], maximum_age_days=550)["evidence"] == []


def test_inline_xbrl_dollar_value_spacing():
    source = filing(filing()["html"].replace("$1,000,000.",
        '$<ix:nonFraction name="dei:EntityPublicFloat">1,000,000</ix:nonFraction>.'))
    result = extract_float(build_history(archive()), source)
    assert result["records"][0]["value"] == 100000


@pytest.mark.parametrize("price,date,usd,accepted", [
    ("10.00", "June 30, 2024", "1,000,000", True),
    ("0", "June 30, 2024", "1,000,000", False),
    ("10", "June 29, 2024", "1,000,000", False),
    ("10", "June 30, 2024", "2,000,000", False),
])
def test_date_first_cover_estimate_after_hidden_xbrl(price, date, usd, accepted):
    head = filing()["html"].split("<p>")[0]
    hidden = '<ix:header>' + ('hidden context ' * 2000) + '</ix:header>'
    text = (f"As of {date}, the aggregate market value of the voting and non-voting common shares "
            f"held by non-affiliates of the registrant, based on the closing price of ${price} per share "
            'as reported on NYSE, was $<ix:nonFraction name="dei:EntityPublicFloat">'
            f"{usd}</ix:nonFraction> (excluded from this figure are affiliates).")
    source = filing(head + hidden + '<p>' + text + '</p></body></html>')
    result = extract_float(build_history(archive()), source)
    assert not result["records"]
    assert bool(result["estimates"]) is accepted
    if accepted:
        row, = result["estimates"]
        assert row["value"] == 100000 and row["effective_date"] == "2024-06-30"
        assert not row["entry_eligible"] and not row["reported_not_estimated"]
        assert build_evidence([result], maximum_age_days=550)["evidence"] == []
    duplicate = filing(head + '<p>' + text + text + '</p></body></html>')
    assert extract_float(build_history(archive()), duplicate)["estimates"] == []


def test_identity_in_hidden_xbrl_is_still_verified():
    source = filing(filing()["html"].replace(
        '<ix:nonNumeric name="dei:EntityCentralIndexKey">0000001800</ix:nonNumeric>',
        '<ix:header><ix:hidden><ix:nonNumeric name="dei:EntityCentralIndexKey">0000001800</ix:nonNumeric>'
        '</ix:hidden></ix:header>'))
    assert len(extract_float(build_history(archive()), source)["records"]) == 1


def parenthetical_filing():
    head = filing()["html"].split("<p>")[0].replace("Common Shares, Without Par Value", "Class A common stock")
    text = ("The aggregate market value of the voting and non-voting common equity held by non-affiliates "
            "of the registrant on June 30, 2024, computed by reference to the price at which the "
            "registrant's Class A common stock was last sold on the New York Stock Exchange on such date "
            "was $1,000,000 (100,000 shares at a closing price per share of $10.00).")
    return filing(head + '<p>' + text + '</p></body></html>')


def test_parenthetical_reported_count_retains_identity_and_availability():
    source = parenthetical_filing()
    result = extract_float(build_history(archive()), source)
    row, = result["records"]
    assert row["value"] == 100000 and row["reported_not_estimated"]
    assert row["basis"] == "reported_nonaffiliate_shares"
    assert row["effective_date"] == "2024-06-30"
    assert row["available_ts"] == build_history(archive())["records"][0]["available_ts"]
    assert not result["estimates"] and not result["abstentions"]
    assert build_evidence([result], maximum_age_days=550)["evidence"][0]["basis"] == "sec_reported_nonaffiliate_proxy"


@pytest.mark.parametrize("old,new", [
    ("100,000 shares", "50,000 shares"),
    ("$10.00", "$0"),
    ("June 30, 2024", "June 29, 2024"),
    ("$1,000,000", "$2,000,000"),
    ("Class A common stock was", "Class B common stock was"),
    ("non-affiliates", "affiliates"),
    ("0000001800</ix", "0000001801</ix"),
    ("</body>", '<ix:nonNumeric name="dei:TradingSymbol">OTHER</ix:nonNumeric></body>'),
])
def test_parenthetical_count_rejects_mismatch(old, new):
    source = filing(parenthetical_filing()["html"].replace(old, new))
    result = extract_float(build_history(archive()), source)
    assert not result["records"] and not result["estimates"]


def test_parenthetical_conflicting_counts_rejected():
    source = parenthetical_filing()
    paragraph = source["html"].split('<p>')[1].split('</p>')[0]
    other = paragraph.replace("100,000 shares", "50,000 shares").replace("$10.00", "$20.00")
    result = extract_float(build_history(archive()), filing(source["html"].replace('</body>', other + '</body>')))
    assert not result["records"]
    assert "conflicting_cover_statements" in result["abstentions"]


@pytest.mark.parametrize("price,accepted", [("10.000004", True), ("10.000006", False)])
def test_parenthetical_rounded_dollars_and_inline_spacing(price, accepted):
    html = parenthetical_filing()["html"].replace("registrant's", "registrant\u2019s")
    html = html.replace("$10.00", "$" + price).replace("$1,000,000",
        '$<ix:nonFraction name="dei:EntityPublicFloat">1,000,000</ix:nonFraction>')
    result = extract_float(build_history(archive()), filing(html))
    assert bool(result["records"]) is accepted


def test_cache_reuses_original_without_credentials(tmp_path):
    history = build_history(archive())
    class Response:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def iter_content(self, size):
            yield filing()["html"].encode()
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert not kwargs["allow_redirects"]
        return Response()
    args = (history, "0000001800-24-000001", "fixture.htm", tmp_path)
    first, reused = cache_filing(*args, user_agent="UnitTest tests@unit.test", get=get)
    assert not reused
    second, reused = cache_filing(*args, get=lambda *a, **kw: pytest.fail("network"))
    assert reused and first == second and len(calls) == 1
    assert "tests@unit.test" not in next(tmp_path.glob("*.json")).read_text()
    assert len(extract_float(history, second)["records"]) == 1
    with pytest.raises(ValueError):
        cache_filing(history, args[1], "../bad.htm", tmp_path)
    with pytest.raises(ValueError):
        cache_filing(history, "0000001800-24-999999", args[2], tmp_path)
