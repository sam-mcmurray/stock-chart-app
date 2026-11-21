import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import qdrant_store


def test_api_import_does_not_load_inference_libraries():
    script = """
import sys
from importlib.abc import MetaPathFinder
import traceback
attempted = []
class RejectML(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'transformers', 'sentence_transformers', 'pandas_ta', 'numba', 'llvmlite'} or fullname == 'drl_agent.agent':
            attempted.append((fullname, ''.join(traceback.format_stack(limit=12))))
            raise AssertionError('Unexpected startup import: ' + fullname)
sys.meta_path.insert(0, RejectML())
import main
from routers import scanners_api
from workers import scanner_worker
import pandas as pd
scanners_api.os.environ.pop('AGENT_WEIGHTS_PATH', None)
assert scanners_api._load_agent(4, 3) is None
assert scanner_worker._macd_open(pd.DataFrame()) is False
assert not attempted, attempted
assert not {'torch', 'transformers', 'sentence_transformers', 'pandas_ta', 'numba', 'llvmlite'} & sys.modules.keys()
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr


def test_embedding_model_is_loaded_on_demand_and_cached(monkeypatch):
    factory = MagicMock()
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    qdrant_store._model.cache_clear()
    try:
        assert qdrant_store.embed_texts([]) == []
        factory.assert_not_called()
        assert qdrant_store._model() is factory.return_value
        assert qdrant_store._model() is factory.return_value
        factory.assert_called_once_with(qdrant_store.EMBEDDER_MODEL)
    finally:
        qdrant_store._model.cache_clear()


def test_embedding_model_load_failure_can_retry(monkeypatch):
    factory = MagicMock(side_effect=[RuntimeError("model unavailable"), object()])
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    qdrant_store._model.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="model unavailable"):
            qdrant_store._model()
        assert qdrant_store._model() is not None
        assert factory.call_count == 2
    finally:
        qdrant_store._model.cache_clear()


def test_agent_loads_existing_weights_on_demand(monkeypatch, tmp_path):
    from routers import scanners_api

    weights = tmp_path / "weights.pt"
    weights.touch()
    monkeypatch.setenv("AGENT_WEIGHTS_PATH", str(weights))
    monkeypatch.setattr(scanners_api, "_AGENT_CACHE", None)
    torch = MagicMock()
    factory = MagicMock()
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "drl_agent.agent", SimpleNamespace(PPOAgent=factory))
    assert scanners_api._load_agent(4, 3) is factory.return_value
    assert scanners_api._load_agent(4, 3) is factory.return_value
    factory.assert_called_once()
    torch.load.assert_called_once_with(str(weights), map_location="cpu")
    factory.return_value.policy.load_state_dict.assert_called_once_with(torch.load.return_value)
    factory.return_value.policy_old.load_state_dict.assert_called_once_with(torch.load.return_value)


@pytest.mark.parametrize("failure", ["missing_dependency", "invalid_weights"])
def test_agent_load_failure_keeps_agent_unavailable(monkeypatch, tmp_path, failure):
    from routers import scanners_api

    weights = tmp_path / "weights.pt"
    weights.touch()
    monkeypatch.setenv("AGENT_WEIGHTS_PATH", str(weights))
    monkeypatch.setattr(scanners_api, "_AGENT_CACHE", None)
    torch = MagicMock()
    torch.load.side_effect = ValueError("invalid weights")
    monkeypatch.setitem(sys.modules, "torch", None if failure == "missing_dependency" else torch)
    monkeypatch.setitem(sys.modules, "drl_agent.agent", SimpleNamespace(PPOAgent=MagicMock()))
    assert scanners_api._load_agent(4, 3) is None
    assert scanners_api._AGENT_CACHE is None


@pytest.mark.parametrize("direction", [1, -1])
def test_macd_keeps_existing_fallback_without_optional_library(monkeypatch, direction):
    import pandas as pd
    from workers import scanner_worker

    monkeypatch.setitem(sys.modules, "pandas_ta", None)
    count = scanner_worker.MACD_SLOW + scanner_worker.MACD_SIGNAL + 50
    frame = pd.DataFrame({"c": [float(direction * i * i) for i in range(count)]})
    assert scanner_worker._macd_open(frame) is (direction == 1)
