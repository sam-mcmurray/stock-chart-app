# Testing the public package

Use an isolated clone or export. Do not run tests against your live brokerage environment or production databases.

## Publication and setup tooling

```bash
python3 scripts/test_publication_tools.py
```

These tests exercise overwrite refusal, private-file exclusions, secret detection, export boundaries, and historical-file checks using temporary synthetic repositories.

## Frontend

From `frontend/`:

```bash
npm ci
npm run test -- --run
npm run lint
npm run build
```

Tests mock backend responses. They do not authorize with Schwab or establish live integration correctness. Build output may report a bundle-size warning; that is separate from build failure.

## Backend fast tests

Use a new Python 3.12 virtual environment, outside any existing installation:

```bash
python3 -m venv /tmp/stockapp-public-tests
/tmp/stockapp-public-tests/bin/pip install -r backend/requirements-test.txt
cd backend
/tmp/stockapp-public-tests/bin/python -m pytest -q test/test_risk.py test/test_agent_api_status.py
```

These fast contracts intentionally require few dependencies. The full suite needs the backend requirements (and, for some tests, additional training or service dependencies). Installing those packages can be substantial. Do not install them into an existing working environment just to validate a public release.

The full suite may be run with `python -m pytest` from `backend/` after preparing an isolated dependency environment. Review integration-test configuration before supplying any database URL. Tests marked for PostgreSQL need a separate disposable database; never point them at retained account or research data.

## Starter validation

Follow [the starter guide](docs/starter-setup.md). `check-starter.py --init` creates a new ignored `.starter` directory; the default checker is read-only. Compose configuration validation does not build images, authorize with Schwab, validate provider permissions, or prove all advanced pipelines work.

The public package does not include the private development Compose test services or operational audit results. A full fresh-image build and live OAuth round trip remain separate checks requiring resources and your own approved credentials.
