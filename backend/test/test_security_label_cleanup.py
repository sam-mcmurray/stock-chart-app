from workers.security_label_cleanup import labels_for_article


def test_labels_for_article_uses_title_and_event_actors():
    calls = []

    def resolver(text):
        calls.append(text)
        return ["GS"]

    labels = labels_for_article(
        {"title": "Quarterly results", "actors": ["GOLDMAN SACHS", None]},
        resolver=resolver,
    )

    assert labels == ["GS"]
    assert calls == ["Quarterly results GOLDMAN SACHS "]
