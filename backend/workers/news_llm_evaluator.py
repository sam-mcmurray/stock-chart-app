"""Bounded synthetic regression checks; never writes article/queue classifications."""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace


def cases():
    # These are invented test inputs, not factual news or training examples.
    rows = [
        ("geography", "Residents meet in the West Bank", "Residents discussed a community picnic and local walking routes.", False, [], None),
        ("politics", "Candidates debate campaign slogans", "The debate concerned campaign colors and personal insults, with no business or economic proposals.", False, [], None),
        ("private", "Cedar Lantern Labs launches inventory service", "Privately owned Cedar Lantern Labs launched an inventory service for small retailers. It has no listed shares.", True, [], None),
        ("product", "Private survey finds gaps in Microsoft 365 account security", "Privately owned Cedar Lantern Labs surveyed office administrators about Microsoft 365 account security. It did not announce a deal with the software vendor.", True, [], None),
        ("tariffs", "Steel tariffs raise manufacturers' input costs", "Manufacturers reported higher steel import costs after tariffs increased. No individual company was identified.", True, [], None),
        ("partnership", "NVIDIA and Microsoft announce cloud computing partnership", "NVIDIA Corporation and Microsoft Corporation announced a cloud computing partnership. NVIDIA will supply processors to Microsoft under the agreement.", True, ["MSFT", "NVDA"], ["partnership", "supplier"]),
        ("japanese", "非上場企業が小売店向け在庫管理サービスを開始", "非上場企業のシーダーランタン社が小売店向けの在庫管理サービスを開始しました。株式は上場していません。", True, [], None),
        ("spanish", "Una empresa privada lanza un servicio para comercios", "La empresa privada Cedar Lantern Labs ha lanzado un servicio de inventario para pequeños comercios. No cotiza en bolsa.", True, [], None),
    ]
    return [{"article": {"id": key, "title": title, "summary": body,
                         "article_text": body, "source": "synthetic-regression",
                         "content_verified": True, "headline_resolution_source": "canonical"},
             "expected": {"relevant": relevant, "tickers": tickers, "relationship": relation,
                          "translated": key in {"japanese", "spanish"}}}
            for key, title, body, relevant, tickers, relation in rows]


def check_result(case, result):
    if not result:
        return ["missing_result"]
    expected = case["expected"]
    errors = []
    relevant = result.get("relevance_label") in {"finance", "business", "markets"}
    if relevant != expected["relevant"] or result.get("relevance_label") == "uncertain":
        errors.append("relevance")
    if set(result.get("related_tickers") or []) != set(expected["tickers"]):
        errors.append("tickers")
    relations = result.get("company_relationships") or []
    if expected["relationship"]:
        if not any(row.get("relationship") in expected["relationship"] and
                   set((row.get("validated_tickers_a") or []) +
                       (row.get("validated_tickers_b") or [])) == set(expected["tickers"])
                   for row in relations):
            errors.append("relationship")
    elif relations:
        errors.append("unexpected_relationship")
    if expected["translated"]:
        if not result.get("translated_to_english"):
            errors.append("translation_flag")
        if result.get("display_headline") == case["article"]["title"]:
            errors.append("untranslated_headline")
    elif result.get("display_headline") != case["article"]["title"]:
        errors.append("publisher_headline_changed")
    if not result.get("llm_summary"):
        errors.append("empty_summary")
    return errors


def evaluate(classifier, sample_cases=None, checker=check_result):
    original_requests = classifier.requests
    calls = []

    def measured_post(*args, **kwargs):
        prompt = kwargs["json"]["messages"][0]["content"]
        metric = {"verification": "\nCORRECTION:" in prompt}
        calls.append(metric)
        start = time.monotonic()
        try:
            response = original_requests.post(*args, **kwargs)
            metric["status"] = response.status_code
            if response.ok:
                payload = response.json()
                for key in ("prompt_eval_count", "eval_count", "total_duration", "eval_duration"):
                    metric[key] = payload.get(key)
                metric["model_content"] = payload.get("message", {}).get("content")
            return response
        finally:
            metric["wall_seconds"] = round(time.monotonic() - start, 3)

    # Replace only this module's reference, never requests.post process-wide.
    classifier.requests = SimpleNamespace(post=measured_post)
    rows = []
    try:
        for case in cases() if sample_cases is None else sample_cases:
            start_index = len(calls)
            article = case["article"]
            started = time.monotonic()
            try:
                result = classifier.classify_articles([article]).get(article["id"], {})
                errors = checker(case, result) + classifier._translation_errors(result)
            except Exception as exc:
                result, errors = {}, [type(exc).__name__ + ": " + str(exc)[:300]]
            rows.append({"id": article["id"], "expected": case.get("expected"),
                         "input": article, "group": case.get("group"),
                         "wall_seconds": round(time.monotonic() - started, 3),
                         "errors": errors, "result": result, "calls": calls[start_index:]})
    finally:
        classifier.requests = original_requests
    return {"policy": classifier.RELEVANCE_POLICY_VERSION, "synthetic": sample_cases is None,
            "limitations": "Small regression set, not corpus accuracy or semantic translation certification. Timings include production contention.",
            "passed": sum(not row["errors"] for row in rows), "total": len(rows),
            "requests": len(calls), "verification_requests": sum(c["verification"] for c in calls),
            "request_wall_seconds": round(sum(c["wall_seconds"] for c in calls), 3),
            "verification_wall_seconds": round(sum(c["wall_seconds"] for c in calls if c["verification"]), 3),
            "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New JSON report path; existing reports are never overwritten")
    args = parser.parse_args()
    path = Path(args.output)
    # Reserve the report before spending inference time; avoid overwriting prior evidence.
    with path.open("x", encoding="utf-8") as handle:
        from services import calendar_relevance
        report = evaluate(calendar_relevance)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}))
    for row in report["rows"]:
        print(row["id"], "PASS" if not row["errors"] else ", ".join(row["errors"]))
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
