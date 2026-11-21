from services import persistent_store


def test_wilson_interval_is_empty_without_reviews():
    assert persistent_store._wilson_interval(0, 0) == (None, None)


def test_wilson_interval_reports_uncertainty_for_small_samples():
    lower, upper = persistent_store._wilson_interval(8, 10)

    assert lower == 49.0
    assert upper == 94.3
    assert lower < 80 < upper


def test_review_categories_have_a_bounded_pilot_target():
    assert persistent_store.NEWS_LLM_REVIEW_TARGET_PER_CATEGORY >= 10
    assert persistent_store.NEWS_LLM_VALIDATION_CATEGORIES == (
        "ticker",
        "relationship",
        "translated",
        "headline_changed",
        "unrelated",
        "unverified",
    )
