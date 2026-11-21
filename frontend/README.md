# Frontend

React 19 / Vite interface for Dashboard, Scanner, Charts, Calendar, News, News LLM, Research, Settings, and Profile. Read the [main guide](../README.md) for the page walkthrough and [testing guide](../TESTING.md) for validation commands.

The starter supplies `VITE_API_BASE=https://127.0.0.1:18443` and serves the UI at `https://127.0.0.1:13000`. These are public service addresses, not secrets. Never put credentials in a `VITE_` variable: its value can be included in browser assets.

The frontend uses session cookies and backend OAuth endpoints. It does not need your brokerage password or app secret. Settings saves scanner preferences; Profile reports connection/worker health and provides disconnect controls.

Run `npm ci`, `npm run test -- --run`, `npm run lint`, and `npm run build` in this directory. For local Vite development against the starter, use the certificate/origin command in the main README and free the frontend port first.
