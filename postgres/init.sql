--
-- Initial database schema for the Stock App.
--
-- This file will be executed by PostgreSQL when the database
-- container is created for the first time.  It can be used to
-- initialise tables, create indexes, or insert seed data.  Feel free
-- to modify or remove this file if you prefer to manage your schema
-- migrations separately.  The example below creates a simple table
-- for storing news articles with an identifier, title, and body.

CREATE TABLE IF NOT EXISTS articles (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS decision_history (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts BIGINT NOT NULL,
    source TEXT NOT NULL,
    watchlist TEXT,
    action TEXT,
    confidence DOUBLE PRECISION,
    score DOUBLE PRECISION,
    catalyst JSONB,
    sentiment JSONB,
    technicals JSONB,
    intraday JSONB,
    trade_plan JSONB,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decision_history_symbol_ts
    ON decision_history (symbol, ts DESC);

CREATE INDEX IF NOT EXISTS idx_decision_history_ts
    ON decision_history (ts DESC);

CREATE TABLE IF NOT EXISTS scanner_settings (
    id TEXT PRIMARY KEY DEFAULT 'default',
    settings JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scanner_results (
    symbol TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    score DOUBLE PRECISION,
    rvol DOUBLE PRECISION,
    ts BIGINT,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scanner_results_bucket_score
    ON scanner_results (bucket, score DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_scanner_results_ts
    ON scanner_results (ts DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS calendar_states (
    session_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_store (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts BIGINT NOT NULL,
    source TEXT NOT NULL,
    price DOUBLE PRECISION,
    action TEXT,
    reward DOUBLE PRECISION,
    semantic_signal JSONB NOT NULL,
    market_signal JSONB NOT NULL,
    stop_loss JSONB NOT NULL,
    committee JSONB,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feature_store_symbol_ts
    ON feature_store (symbol, ts DESC);

CREATE INDEX IF NOT EXISTS idx_feature_store_ts
    ON feature_store (ts DESC);

CREATE TABLE IF NOT EXISTS historical_article_retries (
    url TEXT PRIMARY KEY,
    article JSONB NOT NULL,
    pipeline_version TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_historical_article_retries_due
    ON historical_article_retries (next_attempt_at, created_at);
