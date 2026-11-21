# Public screenshots

The PNGs in `images/` are captures of the actual React frontend, using fictional data supplied by `capture-screenshots.mjs`. They are not screenshots of a connected brokerage account and are not examples of trading performance.

The capture uses a fresh browser context with no saved login state. All backend HTTP requests are fulfilled from synthetic fixtures. Unknown backend routes return an error; external HTTP requests are blocked. All WebSockets are intercepted and closed, including the account stream. Each image includes a synthetic-data label.

## Regenerate

1. Install frontend dependencies with `npm ci` in `frontend/`.
2. Initialize a new `.starter` directory using the setup checker if you need local TLS certificates. No backend or Schwab login is needed; initialization refuses to replace existing files.
3. Start the frontend on the dedicated capture port:

   ```bash
   cd frontend
   VITE_API_BASE=https://127.0.0.1:8443 VITE_TLS_CERT_FILE=../.starter/certs/server.crt VITE_TLS_KEY_FILE=../.starter/certs/server.key npm run dev -- --host 127.0.0.1 --port 3107 --strictPort
   ```

4. In another terminal, install Playwright in a temporary tooling directory (or use an existing installation):

   ```bash
   mkdir -p /tmp/stockapp-screenshot-tools
   npm install --prefix /tmp/stockapp-screenshot-tools playwright
   /tmp/stockapp-screenshot-tools/node_modules/.bin/playwright install chromium
   ```

5. From the directory containing the main README:

   ```bash
   node docs/capture-screenshots.mjs /tmp/stockapp-screenshot-tools/node_modules/playwright/index.mjs
   ```

The script expects `https://127.0.0.1:3107` and the frontend's default API origin `https://127.0.0.1:8443`. It accepts the development certificate only within its isolated browser context. Playwright must support `BrowserContext.routeWebSocket`.

The script exits unsuccessfully if a page throws a JavaScript error or requests a missing fixture. Review the final `unknownEndpoints` and `pageErrors` lists; both should be empty. Then visually inspect every image before publishing. Images capture the top viewport of each screen; longer screens have additional content below the capture.

Do not replace synthetic fixtures with recorded brokerage responses, HAR files, exported browser sessions, real news archives, or production logs. Stop the dedicated frontend server after capturing. The normal application does not gain a demo login or authentication bypass from this documentation script.
