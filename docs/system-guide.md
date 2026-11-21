# Advanced source and system guide

The public starter deploys the interface and core data stores, with optional scanner/account-stream workers. The broader source tree contains additional capabilities that require their own services, data, and operational configuration. The public package intentionally excludes personal deployment records, historical results, research progress/goals, and authenticated portal snapshots.

## News and calendar

`backend/news_sources/` contains provider adapters. `workers/news_worker.py` coordinates ingestion; `services/article_dedup.py` consolidates article coverage. Article indexing and event indexing have separate queues and collections. Canonical GDELT event records should not be treated as duplicate publisher articles.

`services/ollama_manager.py` and `routers/news_llm_api.py` support local news-model management. A separately deployed Ollama service and downloaded compatible model are required. Model selection/download operations can pause processing and remove other installed News LLM models; inspect the implementation before using these controls with existing models. Financial sentiment uses a separate service (`backend/sentiment_service.py`) and route family.

Calendar state is served by `routers/calendar_api.py`. Earnings and IPO workers populate external event context when configured. Personal reviews and planning exports remain private even if their file format is JSON.

## Scanner and decision context

`workers/scanner_worker.py` applies saved filters and gathers context. `services/decision_engine.py` combines technical and catalyst evidence. `services/entry_market_gate.py` checks market conditions used by execution paths. Watchlist labels, scores, and model summaries are decision-support outputs, not orders or guaranteed outcomes.

Float evidence, stale prices, source quality, and market-data coverage affect results. The public source does not include a populated security master, historical corpus, or private observations. Fresh databases will therefore have fewer capabilities until populated by deliberately configured ingestion.

## Research

`backend/drl_agent/` contains data assembly, replay, simulation, training, and evaluation utilities. `services/live_learning_status.py` reports capture/learner status. Dataset assembly and observation capture do not establish that a learner is deployed, an evaluation is leakage-free, or an execution strategy is profitable.

Use each utility's `--help` and read its source before running it. Many tools expect explicit input paths and large datasets. No trained model, local progress snapshot, measured account performance, or archive is included in this repository.

## Trading and protection

The starter omits the order-submission workers and forces live execution gates off. Source modules such as `workers/stop_loss_worker.py`, `workers/protective_stop_worker.py`, and `workers/position_exit_worker.py` require a separate, deliberate deployment and validation process.

Local exit protection depends on the process, market stream, journal, and authorization remaining available. A local trigger is not a broker-held order. Browser logout, worker health, retained authorization, and permission to submit orders are different states. Do not use synthetic screenshots or mocked tests as evidence that live execution is safe.

## Storage and configuration

PostgreSQL stores durable settings, journals, calendar state, and research records. Redis holds session material and recent operational state. Qdrant stores search vectors; Neo4j holds relationship context. Model files and data archives are additional dependencies, not part of the source release.

The initial schema is in `postgres/init.sql`; application startup also initializes required durable schema. Keep database/schema migrations separate from publishing source. The starter's named volumes are isolated from an existing installation and no production migration is performed by the setup checker.

Start with [the main README](../README.md), the [starter guide](starter-setup.md), and [testing instructions](../TESTING.md). Do not copy private credentials, mounts, GPU selections, or research-job arguments from another machine merely to make an advanced tool start.
