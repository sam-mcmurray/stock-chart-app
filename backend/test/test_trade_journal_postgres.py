import hashlib
import os
import uuid

import pytest

from services import persistent_store, trade_journal


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "true",
    reason="set RUN_POSTGRES_INTEGRATION=true to use the isolated PostgreSQL journal test",
)


def _fill(key: str, order_id: int, instruction: str, quantity: int, price: float):
    return {
        "fill_key": hashlib.sha256(key.encode("ascii")).hexdigest(),
        "broker_order_id": order_id,
        "leg_id": 1,
        "symbol": "ZZTEST",
        "instruction": instruction,
        "quantity": quantity,
        "price": price,
        "executed_at": "2026-09-14T15:00:00Z",
    }


def test_repeated_partial_fill_snapshots_rebuild_position_after_restart():
    suffix = uuid.uuid4().hex
    account_id = f"synthetic-restart-test:{suffix}"
    entry_key = f"entry:{suffix}"
    partial_key = f"exit-partial:{suffix}"
    final_key = f"exit-final:{suffix}"
    base_order_id = 800_000_000_000_000_000 + int(suffix[:8], 16)
    position_id = None
    persistent_store.ensure_schema()
    try:
        trade_journal.reserve_order_intent(
            idempotency_key=entry_key,
            account_id=account_id,
            symbol="ZZTEST",
            side="BUY",
            quantity=100,
            order_type="LIMIT",
            order_payload={"orderType": "LIMIT", "price": 10},
            intent_role="entry",
        )
        position_id = trade_journal.ensure_entry_position(
            idempotency_key=entry_key,
            account_id=account_id,
            symbol="ZZTEST",
            intended_quantity=100,
            hard_stop_price=9,
            strategy_version="synthetic-restart-v1",
        )
        trade_journal.update_order_intent(
            entry_key, status="submitted", broker_order_id=base_order_id
        )
        trade_journal.record_order_snapshot({
            "broker_order_id": base_order_id,
            "status": "filled",
            "filled_quantity": 100,
            "remaining_quantity": 0,
            "average_fill_price": 10,
            "raw": {"orderId": base_order_id},
            "fills": [_fill("entry:" + suffix, base_order_id, "BUY", 100, 10)],
        })
        trade_journal.record_order_snapshot({
            "broker_order_id": base_order_id,
            "status": "submitted",
            "filled_quantity": 0,
            "remaining_quantity": 100,
            "average_fill_price": 0,
            "raw": {"orderId": base_order_id, "status": "WORKING"},
            "fills": [],
        })
        with persistent_store.psycopg.connect(
            persistent_store.DATABASE_URL,
            row_factory=persistent_store.dict_row,
        ) as conn:
            reordered_order = dict(conn.execute(
                "SELECT status, filled_quantity, remaining_quantity FROM trade_order_intents WHERE idempotency_key = %s",
                (entry_key,),
            ).fetchone())
            reordered_position = dict(conn.execute(
                "SELECT state, filled_quantity, remaining_quantity FROM trade_positions WHERE position_id = %s",
                (position_id,),
            ).fetchone())
        assert reordered_order == {
            "status": "filled",
            "filled_quantity": 100,
            "remaining_quantity": 0,
        }
        assert reordered_position == {
            "state": "open",
            "filled_quantity": 100,
            "remaining_quantity": 100,
        }

        trade_journal.reserve_order_intent(
            idempotency_key=partial_key,
            position_id=position_id,
            account_id=account_id,
            symbol="ZZTEST",
            side="SELL",
            quantity=50,
            order_type="MARKET",
            order_payload={"orderType": "MARKET"},
            intent_role="exit",
        )
        trade_journal.update_order_intent(
            partial_key, status="submitted", broker_order_id=base_order_id + 1
        )
        first_fill = _fill(
            "partial-1:" + suffix, base_order_id + 1, "SELL", 20, 12
        )
        partial_snapshot = {
            "broker_order_id": base_order_id + 1,
            "status": "partially_filled",
            "filled_quantity": 20,
            "remaining_quantity": 30,
            "average_fill_price": 12,
            "raw": {"orderId": base_order_id + 1},
            "fills": [first_fill],
        }
        first = trade_journal.record_order_snapshot(partial_snapshot)
        replay = trade_journal.record_order_snapshot(partial_snapshot)

        assert first["fills_created"] == 1
        assert replay["fills_created"] == 0

        second_fill = _fill(
            "partial-2:" + suffix, base_order_id + 1, "SELL", 30, 13
        )
        trade_journal.record_order_snapshot({
            **partial_snapshot,
            "status": "filled",
            "filled_quantity": 50,
            "remaining_quantity": 0,
            "average_fill_price": 12.6,
            "fills": [first_fill, second_fill],
        })
        with persistent_store.psycopg.connect(
            persistent_store.DATABASE_URL,
            row_factory=persistent_store.dict_row,
        ) as conn:
            partial_position = dict(conn.execute(
                "SELECT * FROM trade_positions WHERE position_id = %s", (position_id,)
            ).fetchone())
        assert partial_position["state"] == "partial_taken"
        assert partial_position["remaining_quantity"] == 50
        assert partial_position["realized_pnl"] == pytest.approx(130)

        trade_journal.reserve_order_intent(
            idempotency_key=final_key,
            position_id=position_id,
            account_id=account_id,
            symbol="ZZTEST",
            side="SELL",
            quantity=50,
            order_type="MARKET",
            order_payload={"orderType": "MARKET"},
            intent_role="exit",
        )
        trade_journal.update_order_intent(
            final_key, status="submitted", broker_order_id=base_order_id + 2
        )
        trade_journal.record_order_snapshot({
            "broker_order_id": base_order_id + 2,
            "status": "filled",
            "filled_quantity": 50,
            "remaining_quantity": 0,
            "average_fill_price": 11,
            "raw": {"orderId": base_order_id + 2},
            "fills": [
                _fill("final:" + suffix, base_order_id + 2, "SELL", 50, 11)
            ],
        })
        with persistent_store.psycopg.connect(
            persistent_store.DATABASE_URL,
            row_factory=persistent_store.dict_row,
        ) as conn:
            closed_position = dict(conn.execute(
                "SELECT * FROM trade_positions WHERE position_id = %s", (position_id,)
            ).fetchone())
            fill_count = conn.execute(
                "SELECT COUNT(*) FROM trade_fills WHERE position_id = %s", (position_id,)
            ).fetchone()["count"]
        assert closed_position["state"] == "closed"
        assert closed_position["remaining_quantity"] == 0
        assert closed_position["realized_pnl"] == pytest.approx(180)
        assert fill_count == 4
    finally:
        if position_id:
            with persistent_store.psycopg.connect(persistent_store.DATABASE_URL) as conn:
                conn.execute(
                    "DELETE FROM trade_position_events WHERE position_id = %s", (position_id,)
                )
                conn.execute("DELETE FROM trade_fills WHERE position_id = %s", (position_id,))
                conn.execute(
                    "DELETE FROM trade_order_intents WHERE position_id = %s", (position_id,)
                )
                conn.execute("DELETE FROM trade_positions WHERE position_id = %s", (position_id,))
