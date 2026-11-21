from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from routers.auth import require_access_token
from services import persistent_store, sentiment_model_manager


router = APIRouter(prefix="/news/sentiment", tags=["news-sentiment"])


class ModelLoad(BaseModel):
    model: str = Field(min_length=3, max_length=200)
    label_map: dict[str, str] = Field(default_factory=dict)


class SentimentPreview(BaseModel):
    headline: str = Field(min_length=1, max_length=1000)
    summary: str = Field(default="", max_length=10000)


class SentimentEvaluation(SentimentPreview):
    expected_label: str = Field(pattern="^(positive|neutral|negative)$")


class SentimentBenchmarkCase(SentimentEvaluation):
    source_url: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=1000)


class SentimentBenchmarkImport(BaseModel):
    cases: list[SentimentBenchmarkCase] = Field(min_length=1, max_length=500)


def _predict(headline: str, summary: str) -> dict:
    text = f"{headline.strip()}\n{summary.strip()}".strip()
    payload = sentiment_model_manager.predict([text])
    results = payload.get("results") or []
    if not results:
        raise RuntimeError("The sentiment model returned no result")
    return results[0]


@router.get("/status")
def sentiment_status(_access_token: str = Depends(require_access_token)):
    return sentiment_model_manager.status()


@router.get("/catalog")
def sentiment_catalog(
    q: str = Query("", max_length=100),
    _access_token: str = Depends(require_access_token),
):
    try:
        return {"query": q, "models": sentiment_model_manager.catalog(q)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.post("/model")
def load_sentiment_model(body: ModelLoad, _access_token: str = Depends(require_access_token)):
    try:
        return sentiment_model_manager.load(body.model, body.label_map)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:1000]) from exc


@router.delete("/model")
def unload_sentiment_model(_access_token: str = Depends(require_access_token)):
    try:
        return sentiment_model_manager.unload()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.post("/preview")
def preview_sentiment(body: SentimentPreview, _access_token: str = Depends(require_access_token)):
    try:
        return _predict(body.headline, body.summary)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.post("/evaluate")
def evaluate_sentiment(body: SentimentEvaluation, _access_token: str = Depends(require_access_token)):
    try:
        prediction = _predict(body.headline, body.summary)
        saved = persistent_store.save_sentiment_evaluation(
            body.headline,
            body.summary,
            body.expected_label,
            prediction,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc
    if not saved:
        raise HTTPException(status_code=503, detail="PostgreSQL is not configured")
    return saved


@router.get("/evaluations")
def sentiment_evaluations(
    limit: int = Query(20, ge=1, le=100),
    _access_token: str = Depends(require_access_token),
):
    return persistent_store.sentiment_evaluation_summary(limit=limit)


@router.post("/benchmark/cases")
def save_benchmark_case(body: SentimentBenchmarkCase, _access_token: str = Depends(require_access_token)):
    try:
        saved = persistent_store.save_sentiment_benchmark_case(
            body.headline,
            body.summary,
            body.expected_label,
            body.source_url,
            body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not saved:
        raise HTTPException(status_code=503, detail="PostgreSQL is not configured")
    return saved


@router.get("/benchmark/cases")
def benchmark_cases(
    limit: int = Query(500, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _access_token: str = Depends(require_access_token),
):
    return {"cases": persistent_store.list_sentiment_benchmark_cases(limit=limit, offset=offset)}


@router.post("/benchmark/import")
def import_benchmark_cases(
    body: SentimentBenchmarkImport,
    _access_token: str = Depends(require_access_token),
):
    try:
        saved = persistent_store.save_sentiment_benchmark_cases([
            case.model_dump() for case in body.cases
        ])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if saved is None:
        raise HTTPException(status_code=503, detail="PostgreSQL is not configured")
    return {"imported": len(saved), "cases": saved}


@router.delete("/benchmark/cases/{case_key}")
def delete_benchmark_case(case_key: str, _access_token: str = Depends(require_access_token)):
    if not persistent_store.delete_sentiment_benchmark_case(case_key):
        raise HTTPException(status_code=404, detail="Sentiment benchmark case not found")
    return {"deleted": True, "case_key": case_key}


@router.post("/benchmark/run")
def run_sentiment_benchmark(
    limit: int = Query(30, ge=1, le=96),
    _access_token: str = Depends(require_access_token),
):
    cases = persistent_store.list_sentiment_benchmark_cases(limit=limit)
    if not cases:
        return {"evaluated": 0, "summary": persistent_store.sentiment_evaluation_summary()}
    evaluated = 0
    try:
        for start in range(0, len(cases), 32):
            batch = cases[start:start + 32]
            texts = [f"{row['headline']}\n{row.get('summary') or ''}".strip() for row in batch]
            predictions = sentiment_model_manager.predict(texts).get("results") or []
            if len(predictions) != len(batch):
                raise RuntimeError("The sentiment model returned an incomplete benchmark batch")
            for case, prediction in zip(batch, predictions):
                persistent_store.save_sentiment_evaluation(
                    case["headline"], case.get("summary") or "", case["expected_label"], prediction,
                )
                evaluated += 1
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc
    return {"evaluated": evaluated, "summary": persistent_store.sentiment_evaluation_summary()}
