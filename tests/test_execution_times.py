import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from oms_protocol.messages import ExecutionReport, ProtocolError
from oms_protocol.orders import ExecutionEvent

from oms_service.domain import Order, apply_execution
from tests.test_mhi_paper_risk import NOW, intent

FILL_TIME = NOW - timedelta(minutes=7)
COMMAND = SimpleNamespace(operation="submit")


def order():
    return Order(
        order_id="order-1",
        client_order_id="client-1",
        strategy_instance_id="mhi-vwap",
        account_id="paperib",
        intent=intent(),
        command_id="command-1",
        session_token="token-1",
        session_epoch=1,
    )


def report(**changes):
    values = dict(
        event_id="report-1",
        account_id="paperib",
        execution_command_id="command-1",
        order_id="order-1",
        order_revision=1,
        kind="FILL",
        source_session="source-1",
        ingress_seq=1,
        execution_id="fill-1",
        leg_id="MHI2611",
        quantity="1",
        price="25000",
        occurred_at=NOW,
        mode="IB_PAPER",
    )
    return ExecutionReport(**(values | changes))


class ExecutionTimeTests(unittest.TestCase):
    def test_fill_time_is_public_and_survives_oms_persistence_roundtrip(self):
        updated, events, _ = apply_execution(order(), report(executed_at=FILL_TIME), COMMAND)
        self.assertEqual(events[0].executed_at, FILL_TIME)
        loaded = Order.model_validate_json(updated.model_dump_json())
        execution = loaded.view()["executions"]["fill-1"]
        self.assertEqual(execution["executed_at"], FILL_TIME.isoformat())
        self.assertEqual(execution["price"], "25000")
        self.assertEqual(execution["quantity"], "1")
        self.assertFalse(execution["superseded"])
        self.assertNotIn("session_token", loaded.view())

    def test_legacy_report_receipt_time_never_becomes_a_fill_time(self):
        updated, events, _ = apply_execution(order(), report(), COMMAND)
        self.assertIsNone(getattr(events[0], "executed_at", "missing-field"))
        self.assertIsNone(updated.view()["executions"]["fill-1"]["executed_at"])
        # Omitting the new optional field preserves old durable message hashes.
        self.assertNotIn("executed_at", report().model_dump(mode="json"))
        self.assertNotIn("executed_at", events[0].model_dump(mode="json"))
        self.assertIsNone(ExecutionEvent.model_validate(events[0].model_dump()).executed_at)

    def test_fee_updates_keep_the_original_execution_clock(self):
        updated, _, _ = apply_execution(order(), report(executed_at=FILL_TIME), COMMAND)
        fee = report(
            kind="FEE",
            quantity=None,
            price=None,
            leg_id=None,
            fees="25",
            fee_revision=1,
            occurred_at=NOW + timedelta(minutes=20),
        )
        updated, events, _ = apply_execution(updated, fee, COMMAND)
        self.assertEqual(events[0].executed_at, FILL_TIME)
        self.assertEqual(updated.executions["fill-1"]["executed_at"], FILL_TIME.isoformat())
        self.assertEqual(updated.executions["fill-1"]["fees"], "25")
        self.assertEqual(updated.filled["MHI2611"], Decimal(1))

    def test_correction_without_new_broker_time_retains_original_clock(self):
        updated, _, _ = apply_execution(order(), report(executed_at=FILL_TIME), COMMAND)
        corrected, events, _ = apply_execution(
            updated,
            report(
                event_id="report-2",
                execution_id="fill-2",
                correction_of="fill-1",
                price="24999",
                occurred_at=NOW + timedelta(minutes=20),
            ),
            COMMAND,
        )
        self.assertEqual(events[0].executed_at, FILL_TIME)
        self.assertEqual(events[0].update_kind, "CORRECTION")
        self.assertTrue(corrected.view()["executions"]["fill-1"]["superseded"])
        self.assertFalse(corrected.view()["executions"]["fill-2"]["superseded"])
        self.assertEqual(corrected.filled["MHI2611"], Decimal(1))

    def test_explicit_corrected_broker_time_is_authoritative(self):
        updated, _, _ = apply_execution(order(), report(executed_at=FILL_TIME), COMMAND)
        corrected_time = FILL_TIME - timedelta(seconds=1)
        corrected, events, _ = apply_execution(
            updated,
            report(
                event_id="report-2",
                execution_id="fill-2",
                correction_of="fill-1",
                price="24999",
                executed_at=corrected_time,
            ),
            COMMAND,
        )
        self.assertEqual(events[0].executed_at, corrected_time)
        self.assertEqual(corrected.executions["fill-2"]["executed_at"], corrected_time.isoformat())

    def test_broker_recovery_can_supply_a_missing_clock_without_double_filling(self):
        updated, _, _ = apply_execution(order(), report(), COMMAND)
        recovered, events, _ = apply_execution(
            updated,
            report(
                event_id="recovered-time",
                executed_at=FILL_TIME,
            ),
            COMMAND,
        )
        self.assertEqual(recovered.filled["MHI2611"], Decimal(1))
        self.assertEqual(len(recovered.executions), 1)
        self.assertEqual(events[0].executed_at, FILL_TIME)
        duplicate, events, _ = apply_execution(recovered, report(executed_at=FILL_TIME), COMMAND)
        self.assertEqual(duplicate.executions, recovered.executions)
        self.assertEqual(events, [])
        with self.assertRaisesRegex(ProtocolError, "EXECUTION_ID_CONFLICT"):
            apply_execution(recovered, report(executed_at=NOW), COMMAND)

    def test_recovered_clock_preserves_fee_revisions_and_economic_event_values(self):
        updated, _, _ = apply_execution(order(), report(), COMMAND)
        updated, _, _ = apply_execution(
            updated,
            report(
                kind="FEE",
                quantity=None,
                price=None,
                leg_id=None,
                fees="25",
                fee_revision=1,
            ),
            COMMAND,
        )
        recovered, events, _ = apply_execution(
            updated,
            report(
                event_id="recovered-time",
                executed_at=FILL_TIME,
            ),
            COMMAND,
        )
        self.assertEqual(recovered.executions["fill-1"]["fee_revision"], 1)
        self.assertEqual(recovered.executions["fill-1"]["fees"], "25")
        self.assertEqual(events[0].fees, Decimal(25))


if __name__ == "__main__":
    unittest.main()
