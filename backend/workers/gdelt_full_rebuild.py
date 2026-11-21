import json
import logging
import os
from pathlib import Path

from workers.gdelt_event_importer import build_selected_event_id_index, import_events
from workers.gdelt_mentions_importer import import_mentions


def _latest_archive_date(root: str) -> str:
    paths = sorted(Path(root).glob("*.mentions.CSV.zip"))
    return paths[-1].name[:8] if paths else "20260605"


def main():
    logging.basicConfig(level="INFO")
    common = {
        "batch_size": max(1, int(os.getenv("GDELT_IMPORT_BATCH_SIZE", "250"))),
    }
    daily = import_events(
        "/data/gdelt/gdelt_events",
        start_date="20130401",
        state_path="/data/chroma/gdelt_event_import_v2_state.json",
        **common,
    )
    catchup = import_events(
        "/data/gdelt/gdeltv2",
        start_date="20260605",
        state_path="/data/chroma/gdelt_event_import_v2_catchup_state.json",
        **common,
    )
    end_date = _latest_archive_date("/data/gdelt/gdeltv2")
    target_ids, event_id_index = build_selected_event_id_index(
        "/data/gdelt/gdeltv2",
        start_date="20260605",
        end_date=end_date,
        index_path="/data/chroma/gdelt_event_ids_v2_catchup.txt",
        state_path="/data/chroma/gdelt_event_ids_v2_catchup_state.json",
    )
    mentions = import_mentions(
        "/data/gdelt/gdeltv2",
        start_date="20260605",
        end_date=end_date,
        target_ids=target_ids,
        state_path="/data/chroma/gdelt_mentions_v2_catchup_state.json",
    )
    print(json.dumps({
        "daily": daily,
        "catchup": catchup,
        "event_id_index": event_id_index,
        "mentions": mentions,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
