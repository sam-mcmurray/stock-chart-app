"""Conservative volume-only evidence; never a substitute executable price bar."""
import math

from drl_agent.alpaca_data_cache import _time
from drl_agent.alpaca_trade_gap import stamp_ns
from drl_agent.historical_news_join import _verify

VOLUME_POLICY = "alpaca_sip_minute_condition_volume_v2"


def condition_volume(tape, conditions):
    # Minute rules only, strictest condition wins. OTC is intentionally unsupported.
    # https://docs.alpaca.markets/us/docs/market-data-faq#how-are-bars-aggregated
    if tape not in {"A", "B", "C"} or not conditions:
        raise ValueError("Unsupported volume evidence trade conditions")
    price_codes = set("FKLOTX56") | (set(" E") if tape in {"A", "B"} else set("@ABDY"))
    volume_only = set("CHINPRUVZ47") | (set("B") if tape in {"A", "B"} else set("GW"))
    excluded = set("MQ9")
    if not conditions <= price_codes | volume_only | excluded or conditions <= price_codes:
        raise ValueError("Unsupported volume evidence trade conditions")
    return not bool(conditions & excluded)


def volume_minutes(artifacts, symbol):
    output = {}
    for artifact in artifacts:
        _verify(artifact, "alpaca-trade-gap-v1", "archive_sha256")
        request = artifact["request"]
        start = int(_time(request["start"]).timestamp())
        if (artifact["url"] != f"https://data.alpaca.markets/v2/stocks/{symbol}/trades"
                or request.get("feed") != "sip" or request.get("asof") != "-"
                or stamp_ns(request["start"]) != start * 10**9 or start % 60
                or stamp_ns(request["end"]) != (start + 60) * 10**9 - 1):
            raise ValueError("Require matching SIP trade-minute evidence")
        pages = artifact["responses"]
        if not pages or pages[-1].get("next_page_token"):
            raise ValueError("Incomplete trade pagination")
        volume, previous, seen, tokens = 0, None, set(), set()
        for index, page in enumerate(pages):
            if page.get("symbol") != symbol or "trades" not in page:
                raise ValueError("Unexpected trade evidence symbol")
            token = page.get("next_page_token")
            if index < len(pages) - 1 and (not isinstance(token, str) or not token or token in tokens):
                raise ValueError("Invalid trade pagination")
            if token:
                tokens.add(token)
            for trade in page["trades"] or []:
                stamp = stamp_ns(trade["t"])
                identity = (trade["x"], trade["i"], stamp)
                conditions = set(trade["c"])
                counts_volume = condition_volume(trade.get("z"), conditions)
                if (not start * 10**9 <= stamp < (start + 60) * 10**9
                        or previous is not None and stamp < previous or identity in seen
                        or type(trade["s"]) not in (int, float) or not math.isfinite(trade["s"]) or trade["s"] <= 0):
                    raise ValueError("Invalid volume evidence trade")
                previous = stamp
                seen.add(identity)
                if counts_volume:
                    volume += trade["s"]
        row = {"start_ts": start, "available_ts": start + 60, "volume": volume,
               "archive_sha256": artifact["archive_sha256"],
               "basis": VOLUME_POLICY, "retrospective": True}
        if start in output and output[start] != row:
            raise ValueError("Conflicting volume evidence")
        output[start] = row
    return output
