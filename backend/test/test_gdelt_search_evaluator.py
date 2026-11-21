from workers.gdelt_search_evaluator import BENCHMARKS, evaluate_hits, run_benchmark


def test_evaluate_hits_reports_precision_rank_duplicates_and_domains():
    hits = [
        {"title": "Company posts earnings beat", "summary": "", "url": "https://one.test/a", "source": "one.test"},
        {"title": "Unrelated politics", "summary": "", "url": "https://two.test/b", "source": "two.test"},
        {"title": "Revenue rises", "summary": "", "url": "https://one.test/a", "source": "one.test"},
    ]

    result = evaluate_hits(hits, r"earnings|revenue")

    assert result == {
        "precision": 0.667,
        "reciprocal_rank": 1.0,
        "relevant": 2,
        "returned": 3,
        "duplicate_urls": 1,
        "unique_domains": 2,
    }


def test_run_benchmark_uses_injected_search_without_opening_chroma():
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        return 8740000, [
            {
                "title": query,
                "summary": "",
                "url": f"https://example.test/{len(calls)}",
                "source": "example.test",
            }
        ]

    result = run_benchmark(limit=1, search=search)

    assert calls == [(query, 1) for query, _ in BENCHMARKS]
    assert result["collection_count"] == 8740000
    assert result["queries"] == len(BENCHMARKS)
