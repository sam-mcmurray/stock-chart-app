# Separate starter installation

This is an opt-in setup for a new user or a clean public clone. The starter Dockerfile applies cookie isolation inside its own image; the working app source is deliberately left unchanged. Your existing `docker-compose.yml`, `backend/.env`, certificates, containers, databases, and research jobs are not migrated or replaced. You do not need to redo your existing installation.

The standalone `compose.starter.yaml` runs the interface, API, PostgreSQL, Redis, Neo4j, and Qdrant. Optional scanner/account-stream workers use the `scanner` profile. There are no GPU reservations, news-model servers, archive mounts, historical jobs, or order-submission workers. Backend dependencies are still substantial and may download model libraries; this is not a minimal Python distribution.

## Initialize new starter files

From the app directory, run:

```bash
python3 scripts/check-starter.py --init
```

This creates **only a new `.starter/` directory**, containing a copy of the public environment template, a generated encryption key, and self-signed TLS files. It reads no credentials from your existing environment. If `.starter` already exists, it refuses without replacing anything. If generation fails, partial files are retained for inspection.

Edit `.starter/backend.env` locally and supply your own `SCHWAB_CLIENT_ID` and `SCHWAB_CLIENT_SECRET`. Use a separate developer app if you want to leave the existing app's callback configuration unchanged. Register this starter callback:

```text
https://127.0.0.1:18443/auth/callback
```

Trust `.starter/certs/server.crt` through your local browser/OS certificate workflow. Keep its private key and environment file private. All `.starter` content is ignored and excluded from public exports.

## Check before starting

```bash
python3 scripts/check-starter.py
```

The default command is read-only. It checks tools, Docker access, required fields without showing their values, encryption-key format, certificate/key compatibility and expiry, callback IP coverage, port availability, and Compose validation. It never starts services, changes your environment, or refreshes brokerage credentials. Port checks are pre-start checks; they report occupied ports after a starter instance is already running. Network restrictions can prevent Docker/port checks from completing.

## Start explicitly

```bash
docker compose --env-file /dev/null -p stockapp-starter -f compose.starter.yaml up -d --build
```

The explicit project/file selection keeps this separate from your working stack; `/dev/null` avoids loading a root `.env` for Compose interpolation. Backend credentials come only from `.starter/backend.env`. Do not combine this file with `docker-compose.yml` using multiple `-f` arguments.

| Resource | Starter value |
| --- | --- |
| Frontend | `https://127.0.0.1:13000` |
| API | `https://127.0.0.1:18443` |
| OAuth callback | `https://127.0.0.1:18443/auth/callback` |
| Environment | `.starter/backend.env` |
| Certificates | `.starter/certs/` |
| Containers and named volumes | Project prefix `stockapp-starter` |

The starter uses distinct session and CSRF cookies because browsers share cookies between ports on the same hostname. The public frontend export supports `VITE_CSRF_COOKIE_NAME=stockapp_starter_csrf`. When built from the working checkout, `Dockerfile.starter` applies that adjustment inside the starter image, without editing the source on disk.

Only frontend/API ports are published, bound to loopback. Database/cache/vector/graph ports remain internal. The database credentials and disabled graph authentication are development choices for this local network, not a public-hosting configuration. Live-submission gates are forced off and the trading kill switch is forced on by the starter file, regardless of environment-file values.

After login works, optionally start the market/scanner workers:

```bash
docker compose --env-file /dev/null -p stockapp-starter -f compose.starter.yaml --profile scanner up -d --build
```

News, model, and historical-research panels will remain empty or unavailable without those separately configured capabilities. Starting both installations still consumes extra CPU/memory and, when authorized, can create extra API traffic; separate storage does not remove provider limits.

## Inspect or stop only the starter

```bash
docker compose --env-file /dev/null -p stockapp-starter -f compose.starter.yaml --profile scanner ps
docker compose --env-file /dev/null -p stockapp-starter -f compose.starter.yaml --profile scanner stop
```

These commands target the starter project. They do not stop the original project. Avoid `down -v` unless you intend to delete the starter database/model/index volumes.

## Validation scope

The setup script and configuration can be tested without starting containers or connecting to Schwab. A full fresh-image build, OAuth round trip, and provider integration check still require your own approved credentials and a deliberate startup. The starter does not claim those checks have run merely because Compose configuration validates.
