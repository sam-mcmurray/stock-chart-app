# Schwab integration

This project uses your own approved Schwab developer application and your own account authorization. It does not include app credentials, authorization tokens, account data, or snapshots taken from an authenticated developer portal.

## Official documentation

- [Developer Portal](https://developer.schwab.com/)
- [Developer user guides](https://developer.schwab.com/user-guides)
- [Trader API – Individual](https://developer.schwab.com/products/trader-api--individual)
- [Create an app](https://developer.schwab.com/user-guides/apis-and-apps/create-an-app)

Log in to the official portal for the current endpoint contracts, scopes, permissions, rate limits, streaming fields, and usage terms. Do not substitute a bundled historical snapshot for the current provider contract.

## Where to look in the code

| Area | Source |
| --- | --- |
| OAuth exchange and refresh | `backend/auth/schwab_oauth.py` |
| Session and encrypted token storage | `backend/auth/session_store.py` |
| Login, callback, logout, disconnect | `backend/routers/auth.py` |
| Accounts and portfolio data | `backend/schwab/accounts.py`, `backend/routers/portfolio.py` |
| Quotes and price history | `backend/schwab/market_data.py` |
| Streaming | `backend/schwab/streaming_client.py`, `backend/workers/account_stream_worker.py` |
| Orders and transactions | `backend/schwab/orders.py`, `backend/schwab/transactions.py` |

The starter callback is `https://127.0.0.1:18443/auth/callback`, with the frontend at `https://127.0.0.1:13000`. Register the exact callback and keep the browser origin, CORS configuration, and local certificate aligned. Follow [the starter guide](starter-setup.md).

Brokerage passwords are entered only on Schwab's official login page. App secrets and the token-encryption key remain in `.starter/backend.env`. Do not disclose callback query strings, session cookies, account identifiers, or raw account API responses in issues.

The starter disables order submission. Market-data/scanner functionality and trading authorization are distinct. Auto Trader credential retention can survive browser logout; use the app's disconnect control to remove retained application credentials.
