import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import websockets
from websockets.exceptions import ConnectionClosed

from schwab.user_preferences import UserPreferences

TokenProvider = Union[str, Callable[[], str], Callable[[], Awaitable[str]]]

def _now() -> int:
    return int(time.time())

def _as_list(x) -> List[str]:
    if x is None: return []
    if isinstance(x, (list, tuple, set)): return [str(i) for i in x]
    return [str(x)]

async def _maybe_await(fn, *args, **kwargs):
        res = fn(*args, **kwargs)
        if asyncio.iscoroutine(res):
            await res

class SchwabWebSocketClient:
    """
    General-purpose Schwab Streamer client with SUBS/UNSUBS and auto-resubscribe.
    """

    def __init__(
        self,
        user_prefs: UserPreferences,
        token_provider: TokenProvider,
        *,
        ping_interval: float = 20.0,
        ping_timeout: float = 30.0,
        max_backoff: float = 30.0,
    ):
        creds = user_prefs.get_streamer_credentials() or {}

        # Map your fields (adjust keys to match your UserPreferences)
        self.url         = creds.get("socket_url")
        self.customer_id = creds.get("customer_id")
        self.correl_id   = creds.get("correlation_id")
        self.channel     = creds.get("channel")
        self.function_id = creds.get("function_id")

        if not self.url or not self.customer_id or not self.correl_id:
            raise ValueError("Missing required streamer credentials (url/customer_id/correl_id).")

        self._token_provider = token_provider
        self._access_token: Optional[str] = None

        # ws runtime
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._rid = 0
        self._last_seq: Dict[str, int] = {}  # per-service/key sequence dedup
        self._subs: List[Dict[str, Any]] = []  # stored subscriptions for resubscribe
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_backoff = max_backoff

    # ----------------------
    # Internals
    # ----------------------

    async def _refresh_token(self):
        """Get or refresh access token via the provided token_provider."""
        prov = self._token_provider
        if isinstance(prov, str):
            self._access_token = prov
            return
        res = prov()
        self._access_token = await res if asyncio.iscoroutine(res) else res

    async def _login(self):
        """Send ADMIN/LOGIN and wait for Schwab's success response."""
        if not self.websocket:
            raise RuntimeError("WebSocket is not connected")

        params = {
            "Authorization": self._access_token,
            "SchwabClientChannel": self.channel,
            "SchwabClientFunctionId": self.function_id,
        }
        login = {
            "requests": [
                {
                    "service": "ADMIN",
                    "command": "LOGIN",
                    "requestid": self._next_rid(),
                    "SchwabClientCustomerId": str(self.customer_id),
                    "SchwabClientCorrelId": str(self.correl_id),
                    "parameters": params,
                }
            ]
        }
        await self._send(login)
        deadline = asyncio.get_running_loop().time() + 15.0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Schwab streamer login response")
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            for response in message.get("response") or []:
                if response.get("service") != "ADMIN" or response.get("command") != "LOGIN":
                    continue
                content = response.get("content") or {}
                code = int(content.get("code", -1))
                if code != 0:
                    detail = content.get("msg") or "login denied"
                    raise PermissionError(f"Schwab streamer login failed ({code}): {detail}")
                return

    def _mk_request(self, *, service: str, command: str, keys: Union[str, List[str]], fields: Union[str, List[int], List[str]]):
        k = ",".join(_as_list(keys))
        f = fields if isinstance(fields, str) else ",".join(map(str, _as_list(fields)))
        return {
            "requests": [
                {
                    "service": service,
                    "command": command,
                    "requestid": self._next_rid(),
                    "SchwabClientCustomerId": str(self.customer_id),
                    "SchwabClientCorrelId": str(self.correl_id),
                    "parameters": {
                        "keys": k,
                        "fields": f
                    }
                }
            ]
        }

    async def _resubscribe_all(self):
        """Re-apply stored SUBS after reconnect."""
        for sub in list(self._subs):
            req = self._mk_request(service=sub["service"], command="SUBS", keys=sub["keys"], fields=sub["fields"])
            await self._send(req)

    def _remember_sub(self, service: str, keys: Union[str, List[str]], fields: Union[str, List[int], List[str]]):
        # SUBS replaces the complete symbol set for a service.
        k = ",".join(_as_list(keys))
        self._subs = [s for s in self._subs if s["service"] != service]
        self._subs.append({
            "service": service,
            "keys": k,
            "fields": fields if isinstance(fields, str) else ",".join(map(str, _as_list(fields))),
        })

    def _forget_sub(self, service: str, keys: Union[str, List[str]]):
        k = ",".join(_as_list(keys))
        self._subs = [s for s in self._subs if not (s["service"] == service and s["keys"] == k)]

    async def _send(self, obj: Dict[str, Any]):
        if not self.websocket:
            raise RuntimeError("WebSocket is not connected")
        await self.websocket.send(json.dumps(obj))

    async def _dispatch_data(self, items: List[Dict[str, Any]], on_data):
        """
        Schwab packets usually look like:
          {"data":[{"service":"ACCT_ACTIVITY","timestamp":...,"content":[{"seq":123,"key":"...","1":"...","2":"..."}]}]}
        """
        for item in items:
            service = item.get("service", "")
            for row in item.get("content") or []:
                seq = int(row.get("seq", -1)) if "seq" in row else -1
                if seq >= 0:
                    bucket = self._sequence_bucket(service, row)
                    last = self._last_seq.get(bucket)
                    if last is not None and seq <= last:
                        # duplicate or old message during reconnect; skip
                        continue
                    self._last_seq[bucket] = seq
                await _maybe_await(on_data, service, row)

    @staticmethod
    def _sequence_bucket(service: str, row: Dict[str, Any]) -> str:
        key = str(row.get("key") or row.get("0") or "")
        return f"{service}\x1f{key}" if key else service

    def _clear_service_sequences(self, service: str) -> None:
        prefix = f"{service}\x1f"
        for bucket in list(self._last_seq):
            if bucket == service or bucket.startswith(prefix):
                self._last_seq.pop(bucket, None)

    def _apply_control_responses(self, responses: List[Dict[str, Any]]) -> None:
        for response in responses:
            if str(response.get("command") or "").upper() != "SUBS":
                continue
            content = response.get("content") or {}
            try:
                succeeded = int(content.get("code", -1)) == 0
            except (TypeError, ValueError):
                succeeded = False
            if succeeded:
                # A replacement SUBS starts a fresh delivery sequence for the
                # service. Clear only after Schwab acknowledges the replacement
                # so an in-flight packet from the prior subscription cannot set
                # the new high-water mark.
                self._clear_service_sequences(str(response.get("service") or ""))

    def _next_rid(self) -> str:
        self._rid += 1
        return str(self._rid)
    
    def _make_symbols(self, symbols):
    # Schwab expects comma-separated, upper-cased symbols
        if isinstance(symbols, (list, tuple, set)):
            return ",".join(str(s).upper() for s in symbols)
        return str(symbols).upper()

    # ----------------------
    # Public API
    # ----------------------

    async def connect(self):
        """Open the websocket and LOGIN."""
        await self._refresh_token()
        self.websocket = await websockets.connect(
            self.url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=2**20,
        )
        await self._login()
        # Stream sequence numbers are scoped to a socket session. Retaining the
        # previous connection's high-water marks can suppress every row after
        # Schwab restarts a service sequence on reconnect.
        self._last_seq.clear()
        # Re-subscribe any prior subscriptions (e.g., after reconnect)
        if self._subs:
            await self._resubscribe_all()

    async def close(self):
        if self.websocket:
            try:
                await self.websocket.close()
            finally:
                self.websocket = None

    async def run(
        self,
        on_data: Callable[[str, Dict[str, Any]], Awaitable[None]] | Callable[[str, Dict[str, Any]], None],
        on_response: Optional[Callable[[Dict[str, Any]], Awaitable[None] | None]] = None,
        on_error: Optional[Callable[[str], Awaitable[None] | None]] = None,
    ):
        """
        Main loop with auto-reconnect and backoff.
        Calls:
          - on_data(service, row) for each data row
          - on_response(resp) for control/ack messages
          - on_error(message) on exceptions
        """
        backoff = 1.0
        while True:
            try:
                if not self.websocket:
                    await self.connect()
                backoff = 1.0  # reset backoff after successful connect
                # Listen until disconnect
                async for raw in self.websocket:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        # Non-JSON frame (rare): surface raw
                        if on_error:
                            await _maybe_await(on_error, f"Non-JSON frame: {raw!r}")
                        continue

                    # Control/ack path
                    if "response" in msg:
                        self._apply_control_responses(msg["response"])
                        if on_response:
                            await _maybe_await(on_response, msg["response"])
                    # Data path
                    if "data" in msg:
                        await self._dispatch_data(msg["data"], on_data)

            except ConnectionClosed:
                # reconnect
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            except Exception as e:
                if on_error:
                    await _maybe_await(on_error, f"stream error: {e}")
                # brief wait then try reconnect
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            finally:
                await self.close()

    async def subscribe(self, service: str, keys: Union[str, List[str]], fields: Union[str, List[int], List[str]]):
        """Generic SUBS to any service."""
        req = self._mk_request(service=service, command="SUBS", keys=keys, fields=fields)
        await self._send(req)
        # store for resubscribe
        self._remember_sub(service, keys, fields)

    async def unsubscribe(self, service: str, keys: Union[str, List[str]]):
        """Generic UNSUBS."""
        req = self._mk_request(service=service, command="UNSUBS", keys=keys, fields=[])
        await self._send(req)
        self._forget_sub(service, keys)

    # Convenience helpers
    async def subscribe_levelone_equities(self, symbols: List[str], fields: List[int] | List[str]):
        await self.subscribe("LEVELONE_EQUITIES", symbols, fields)

    async def subscribe_acct_activity(self, label: str = "Account Activity", fields: Union[str, List[int], List[str]] = "0,1,2,3"):
        await self.subscribe("ACCT_ACTIVITY", label, fields)

    # ---------- NASDAQ Level II ----------
    async def subscribe_nasdaq_book(self, symbols):
        await self.subscribe("NASDAQ_BOOK", symbols, "0,1,2,3")

    async def add_nasdaq_book(self, symbols):
        await self._send(self._mk_request(service="NASDAQ_BOOK", command="ADD", keys=symbols, fields="0,1,2,3"))

    async def unsubscribe_nasdaq_book(self, symbols):
        await self.unsubscribe("NASDAQ_BOOK", symbols)

    # ---------- NYSE / Listed Level II ----------
    # Schwab book docs/wrappers refer to the NYSE/“listed” book. On wire this is commonly 'LISTED_BOOK'.
    async def subscribe_nyse_book(self, symbols):
        await self.subscribe("NYSE_BOOK", symbols, "0,1,2,3")

    async def add_nyse_book(self, symbols):
        await self._send(self._mk_request(service="NYSE_BOOK", command="ADD", keys=symbols, fields="0,1,2,3"))

    async def unsubscribe_nyse_book(self, symbols):
        await self.unsubscribe("NYSE_BOOK", symbols)

    # ---------- OPTIONS Level II ----------
    async def subscribe_options_book(self, option_symbols):
        await self.subscribe("OPTIONS_BOOK", option_symbols, "0,1,2,3")

    async def add_options_book(self, option_symbols):
        await self._send(self._mk_request(service="OPTIONS_BOOK", command="ADD", keys=option_symbols, fields="0,1,2,3"))

    async def unsubscribe_options_book(self, option_symbols):
        await self.unsubscribe("OPTIONS_BOOK", option_symbols)
    
    # ---------- CHART: Equity ----------
    async def subscribe_chart_equity(self, symbols, fields: str | list[str|int] = "0,1,2,3,4,5,6,7,8"):
        await self.subscribe("CHART_EQUITY", symbols, fields)

    async def add_chart_equity(self, symbols, fields: str | list[str|int] = "0,1,2,3,4,5,6,7,8"):
        await self._send(self._mk_request(service="CHART_EQUITY", command="ADD", keys=symbols, fields=fields))

    async def unsubscribe_chart_equity(self, symbols):
        await self.unsubscribe("CHART_EQUITY", symbols)

    # Observed production wire data:
    # 0 key, 1 sequence, 2 open, 3 high, 4 low, 5 close, 6 volume, 7 chartTime(ms), 8 chartDay

    # ---------- CHART: Futures ----------
    async def subscribe_chart_futures(self, futures_symbols, fields: str | list[str|int] = "0,1,2,3,4,5,6"):
        await self.subscribe("CHART_FUTURES", futures_symbols, fields)

    async def add_chart_futures(self, futures_symbols, fields: str | list[str|int] = "0,1,2,3,4,5,6"):
        await self._send(self._mk_request(service="CHART_FUTURES", command="ADD", keys=futures_symbols, fields=fields))

    async def unsubscribe_chart_futures(self, futures_symbols):
        await self.unsubscribe("CHART_FUTURES", futures_symbols)

    # Field meanings (incoming):
    # 0 key, 1 chartTime(ms), 2 open, 3 high, 4 low, 5 close, 6 volume

    # ---------- SCREENER helpers ----------
    # keys are PREFIX_SORTFIELD_FREQUENCY (comma-separated for multiple keys)
    # e.g.: 'NASDAQ_VOLUME_1', 'INDEX_ALL_PERCENT_CHANGE_UP_0'
    async def subscribe_screener_equity(self, keys, fields: str | list[str|int] = "0,1,2,3,4"):
        await self.subscribe("SCREENER_EQUITY", keys, fields)

    async def add_screener_equity(self, keys, fields: str | list[str|int] = "0,1,2,3,4"):
        await self._send(self._mk_request(service="SCREENER_EQUITY", command="ADD", keys=keys, fields=fields))

    async def unsubscribe_screener_equity(self, keys):
        await self.unsubscribe("SCREENER_EQUITY", keys)

    async def subscribe_screener_option(self, keys, fields: str | list[str|int] = "0,1,2,3,4"):
        await self.subscribe("SCREENER_OPTION", keys, fields)

    async def add_screener_option(self, keys, fields: str | list[str|int] = "0,1,2,3,4"):
        await self._send(self._mk_request(service="SCREENER_OPTION", command="ADD", keys=keys, fields=fields))

    async def unsubscribe_screener_option(self, keys):
        await self.unsubscribe("SCREENER_OPTION", keys)

# # Subscribe/unsubscribe LEVELONE_EQUITIES
# client = SchwabWebSocketClient()
# await client.subscribe("LEVELONE_EQUITIES", keys=["AAPL","TSLA"], fields=[0,1,2,3])
# await client.unsubscribe("LEVELONE_EQUITIES", keys=["TSLA"])

# # Subscribe account activity
# await client.subscribe_acct_activity(label="Account Activity", fields="0,1,2,3")
