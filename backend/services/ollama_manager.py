import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup


OLLAMA_URL = os.getenv("NEWS_OLLAMA_URL", "http://ollama-news:11434").rstrip("/")
SETTINGS_KEY = "news:llm:settings"
DEFAULT_MODEL = os.getenv("NEWS_LLM_DEFAULT_MODEL", "")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?$")
CATALOG_FALLBACK = [
    {"name": "gemma3", "description": "Google Gemma 3 model family.", "url": "https://ollama.com/library/gemma3"},
    {"name": "llama3.2", "description": "Meta Llama 3.2 model family.", "url": "https://ollama.com/library/llama3.2"},
    {"name": "phi4-mini", "description": "Microsoft Phi-4 Mini model.", "url": "https://ollama.com/library/phi4-mini"},
]
CAPABILITY_LABELS = {"audio", "cloud", "embedding", "thinking", "tools", "vision"}


def _model_name(value: str) -> str:
    model = str(value or "").strip()
    if not MODEL_NAME_RE.fullmatch(model):
        raise ValueError("Invalid Ollama model name")
    variant = model.rsplit(":", 1)[-1].lower()
    if variant == "cloud" or variant.endswith(("-cloud", "-mlx")):
        raise ValueError("Select a local non-MLX model variant for the NVIDIA News LLM container")
    return model


def get_settings(redis_client) -> dict:
    stored = redis_client.hgetall(SETTINGS_KEY) or {}
    return {
        "enabled": stored.get("enabled", "0") == "1",
        "active_model": stored.get("active_model") or DEFAULT_MODEL,
    }


def update_settings(redis_client, *, enabled: bool | None = None, active_model: str | None = None) -> dict:
    updates = {}
    if enabled is not None:
        updates["enabled"] = "1" if enabled else "0"
    if active_model is not None:
        updates["active_model"] = _model_name(active_model)
    if updates:
        redis_client.hset(SETTINGS_KEY, mapping=updates)
    return get_settings(redis_client)


def _request(method: str, path: str, *, timeout: float = 10, **kwargs):
    response = requests.request(method, f"{OLLAMA_URL}{path}", timeout=timeout, **kwargs)
    if not response.ok:
        try:
            body = response.json()
            detail = body.get("error") or body.get("message") or str(body)
        except Exception:
            detail = response.text
        detail = " ".join(str(detail or "Unknown Ollama error").split())[:500]
        raise RuntimeError(f"Ollama request failed ({response.status_code}): {detail}")
    if not response.content:
        return {}
    return response.json()


def list_models() -> list[dict]:
    return list((_request("GET", "/api/tags").get("models") or []))


def running_models() -> list[dict]:
    return list((_request("GET", "/api/ps").get("models") or []))


def server_status(redis_client) -> dict:
    settings = get_settings(redis_client)
    try:
        version = _request("GET", "/api/version").get("version")
        models = list_models()
        running = running_models()
        available = True
        error = None
    except requests.ConnectionError:
        version, models, running, available = None, [], [], False
        error = "Ollama is offline. Start the ollama-news Compose service after configuring NVIDIA Container Toolkit."
    except Exception as exc:
        version, models, running, available = None, [], [], False
        error = f"Ollama status check failed: {str(exc)[:240]}"
    installed_names = {row.get("name") or row.get("model") for row in models}
    return {
        **settings,
        "available": available,
        "version": version,
        "models": models,
        "running_models": running,
        "active_model_installed": settings["active_model"] in installed_names,
        "endpoint": OLLAMA_URL,
        "host_port": int(os.getenv("NEWS_OLLAMA_HOST_PORT", "11435")),
        "gpu_device": os.getenv("NEWS_OLLAMA_GPU_DEVICE", "0"),
        "error": error,
    }


def pull_model(model: str) -> dict:
    model = _model_name(model)
    result = _request(
        "POST",
        "/api/pull",
        timeout=float(os.getenv("NEWS_OLLAMA_PULL_TIMEOUT_SECONDS", "3600")),
        json={"model": model, "stream": False},
    )
    return {"model": model, **result}


def delete_model(model: str) -> dict:
    model = _model_name(model)
    _request("DELETE", "/api/delete", json={"model": model})
    return {"ok": True, "model": model}


def delete_unselected_models(selected_model: str) -> list[str]:
    selected_model = _model_name(selected_model)
    removed = []
    for row in list_models():
        installed = str(row.get("name") or row.get("model") or "")
        if installed and installed != selected_model:
            delete_model(installed)
            removed.append(installed)
    return removed


def _catalog_summary(anchor) -> dict:
    name_node = anchor.select_one("h2")
    name = str(name_node.get_text(" ", strip=True) if name_node else "").strip()
    description_node = anchor.select_one("h2 + p")
    description = str(description_node.get_text(" ", strip=True) if description_node else "").strip()
    badge_container = anchor.select_one("div.flex.flex-wrap")
    badges = [
        " ".join(span.get_text(" ", strip=True).split())
        for span in (badge_container.select("span") if badge_container else [])
        if span.get_text(" ", strip=True)
    ]
    capabilities = [badge for badge in badges if badge.lower() in CAPABILITY_LABELS]
    parameter_sizes = [badge for badge in badges if badge.lower() not in CAPABILITY_LABELS]
    text = " ".join(anchor.get_text(" ", strip=True).split())
    pulls_match = re.search(r"([\d.]+[KMB]?)\s+Pulls", text, re.IGNORECASE)
    tags_match = re.search(r"(\d+)\s+Tags", text, re.IGNORECASE)
    updated_match = re.search(r"Updated\s+(.+)$", text, re.IGNORECASE)
    updated_node = anchor.select_one('[title*="UTC"]')
    href = str(anchor.get("href") or "")
    return {
        "name": name,
        "description": description,
        "capabilities": capabilities,
        "parameter_sizes": parameter_sizes,
        "pulls": pulls_match.group(1) if pulls_match else None,
        "tag_count": int(tags_match.group(1)) if tags_match else None,
        "updated": updated_match.group(1) if updated_match else None,
        "updated_at": updated_node.get("title") if updated_node else None,
        "url": urljoin("https://ollama.com", href),
    }


def _catalog_details(row: dict) -> dict:
    try:
        response = requests.get(
            row["url"],
            timeout=15,
            headers={"User-Agent": "StockChartApp/1.0"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        prefix = f"/library/{row['name']}:"
        variants = {}
        for anchor in soup.select(f'a[href^="{prefix}"]'):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            if "context window" not in text:
                continue
            name_node = anchor.select_one("p.font-medium")
            model_name = (
                " ".join(name_node.get_text(" ", strip=True).split())
                if name_node else str(anchor.get("href") or "").removeprefix("/library/")
            )
            meta_nodes = [
                " ".join(node.get_text(" ", strip=True).split())
                for node in anchor.select("p")
                if "context window" in node.get_text(" ", strip=True)
            ]
            parts = [part.strip() for part in (meta_nodes[0].split("·") if meta_nodes else [])]
            is_cloud = model_name.endswith("-cloud") or (parts and "Usage" in parts[0])
            is_mlx = model_name.endswith("-mlx")
            variants[model_name] = {
                "name": model_name,
                "size": parts[0] if len(parts) > 0 else None,
                "context_window": parts[1].replace(" context window", "") if len(parts) > 1 else None,
                "input": parts[2] if len(parts) > 2 else None,
                "updated": parts[3] if len(parts) > 3 else None,
                "distribution": "cloud" if is_cloud else "mlx" if is_mlx else "local",
                "local_compatible": not is_cloud and not is_mlx,
            }
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        requirement_match = re.search(
            r"(This model requires Ollama\s+[0-9.]+\s+or later\.)",
            page_text,
            re.IGNORECASE,
        )
        return {
            **row,
            "variants": list(variants.values()),
            "requirements": requirement_match.group(1) if requirement_match else None,
        }
    except Exception:
        return {**row, "variants": [], "requirements": None}


def search_catalog(query: str) -> list[dict]:
    needle = str(query or "").strip().lower()
    fallback = [
        row for row in CATALOG_FALLBACK
        if not needle or needle in row["name"].lower() or needle in row["description"].lower()
    ]
    if len(needle) < 2:
        return fallback
    try:
        response = requests.get(
            f"https://ollama.com/search?q={quote_plus(needle)}",
            timeout=10,
            headers={"User-Agent": "StockChartApp/1.0"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        found = {}
        for anchor in soup.select('a[href^="/library/"]'):
            row = _catalog_summary(anchor)
            name = row["name"]
            if not name or "/" in name:
                continue
            found[name] = row
        rows = list(found.values())[:12]
        if not rows:
            return fallback
        with ThreadPoolExecutor(max_workers=min(6, len(rows))) as executor:
            return list(executor.map(_catalog_details, rows))
    except Exception:
        return fallback
