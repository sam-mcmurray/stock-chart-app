import pytest
import subprocess
from types import SimpleNamespace

import sentiment_service


def test_known_finbert_label_map_is_normalized():
    mapping = sentiment_service._normalize_label_map(
        "yiyanghkust/finbert-tone",
        {},
        {0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"},
    )
    assert mapping == {"label_0": "neutral", "label_1": "positive", "label_2": "negative"}


def test_custom_generic_labels_require_explicit_map():
    with pytest.raises(ValueError, match="explicit"):
        sentiment_service._normalize_label_map(
            "owner/custom-model",
            {},
            {0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"},
        )


def test_semantic_model_labels_need_no_custom_map():
    mapping = sentiment_service._normalize_label_map(
        "owner/safe-model",
        {},
        {0: "negative", 1: "neutral", 2: "positive"},
    )
    assert mapping == {"positive": "positive", "neutral": "neutral", "negative": "negative"}


def test_health_does_not_select_a_default_model(monkeypatch):
    monkeypatch.setattr(sentiment_service, "_model", None)
    monkeypatch.setattr(sentiment_service, "_model_id", "")
    monkeypatch.setattr(sentiment_service, "CUDA_READY", False)

    result = sentiment_service.health()

    assert result["loaded"] is False
    assert result["model"] is None
    assert result["requested_device"] in {"cpu", "cuda"}


def test_cuda_probe_times_out_to_cpu(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=3)

    monkeypatch.setattr(sentiment_service.subprocess, "run", timeout)

    ready, error, gpu_name, arches = sentiment_service._probe_cuda(timeout_seconds=3)

    assert ready is False
    assert error == "CUDA validation exceeded 3s; using CPU"
    assert gpu_name is None
    assert arches == []


def test_cuda_probe_returns_cached_device_details(monkeypatch):
    payload = '{"ready": true, "error": "", "gpu_name": "RTX", "cuda_arches": ["sm_120"]}'
    monkeypatch.setattr(
        sentiment_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    ready, error, gpu_name, arches = sentiment_service._probe_cuda()

    assert ready is True
    assert error == ""
    assert gpu_name == "RTX"
    assert arches == ["sm_120"]
