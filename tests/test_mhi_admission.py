import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from oms_protocol.common import digest
from oms_protocol.messages import RiskRequest
from oms_protocol.orders import OrderIntent

from oms_service.domain import Order
from oms_service.recovery import Recovery
from risk_service.ledger import empty_order
from risk_service.service import Service
from tests.test_mhi_paper_risk import NOW, approved_settings, intent


def risk_order(identity, order_intent=None, *, filled=False, pending=False, owner="mhi-vwap"):
    order_intent = order_intent or intent()
    name = order_intent.legs[0].instrument_id
    view = Order(
        order_id=identity,
        client_order_id=identity,
        strategy_instance_id=owner,
        account_id="default",
        intent=order_intent,
        command_id=identity,
        session_token="token",
        session_epoch=1,
        status="FILLED" if filled else "SUBMITTED" if pending else "RISK_PENDING",
        filled={name: "1" if filled else "0"},
        pending=None
        if filled or pending
        else dict(operation="submit", revision=1, request_id="request-1"),
    ).view()
    data = empty_order() | dict(view=view, ever_filled=filled)
    if filled or pending:
        data["reservations"] = {
            identity: dict(
                revision=1,
                cost="500100",
                quantity="1",
                fee_buffer="100",
                active=pending,
                definitely_not_executed=False,
                paper_signal_only=False,
            )
        }
    if filled:
        data["executions"] = {
            identity: dict(
                leg_id=name,
                quantity="1",
                price="25000",
                fees="0",
                superseded=False,
            )
        }
    return data


class RiskStore:
    def __init__(self, orders):
        self.db = self
        self.orders = orders
        self.metadata = dict(feed_epoch="feed-1", account_seq=0)
        self.writes = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def meta(self, key, default=None):
        return self.metadata.get(key, default)

    async def set_meta(self, key, value):
        self.metadata[key] = value

    async def one(self, sql, params=()):
        if "count(*) AS account" in sql:
            return dict(account=0, strategy=0)
        if "count(*) AS n FROM risk.outbox" in sql:
            return dict(n=0)
        if "SELECT hash,decision FROM risk.requests" in sql:
            return None
        raise AssertionError(sql)

    async def all(self, sql):
        if sql == "SELECT id,data FROM risk.orders":
            return [dict(id=key, data=data) for key, data in self.orders.items()]
        raise AssertionError(sql)

    async def order(self, identity):
        return self.orders[identity]

    async def save_order(self, identity, data):
        self.orders[identity] = data

    async def execute(self, sql, params):
        self.writes.append((sql, params))


class MhiAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def decide(self, other_orders=(), *, closing=None, external_hold=None):
        order_intent = closing or intent()
        orders = dict(other_orders) | {"new-order": risk_order("new-order", order_intent)}
        store = RiskStore(orders)
        # Keep monetary headroom to test the independent two-contract admission bound.
        config = approved_settings(
            account_by_currency={"HKD": dict(max_order_cost="500100", max_committed="9999999")},
            strategies_by_currency={
                "mhi-vwap": {"HKD": dict(max_order_cost="500100", max_committed="9999999")}
            },
        )
        request = RiskRequest(
            account_id="default",
            feed_epoch="feed-1",
            message_id="request-1",
            order_id="new-order",
            strategy_instance_id="mhi-vwap",
            order_revision=1,
            intent=order_intent,
            intent_hash=digest(order_intent.model_dump(mode="json")),
            required_account_seq=0,
            operation="submit",
            mode="IB_PAPER",
        )
        with patch("risk_service.service.utcnow", return_value=NOW):
            decision = await Service(config, store).decide(request, external_hold=external_hold)
        return decision, store

    async def test_reservation_is_monetary_and_second_contract_can_open(self):
        decision, store = await self.decide([("first", risk_order("first", filled=True))])
        self.assertTrue(decision.approved, decision.reason)
        reservation = store.orders["new-order"]["reservations"]["request-1"]
        self.assertEqual(reservation["cost"], "500100")
        self.assertFalse(reservation["paper_signal_only"])

    async def test_pending_open_plus_filled_lot_prevent_a_third_contract(self):
        decision, _ = await self.decide(
            [
                ("first", risk_order("first", filled=True)),
                ("second", risk_order("second", pending=True)),
            ]
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "MARKET_POSITION_LIMIT")

    async def test_owned_market_close_is_allowed_at_the_position_limit_without_quotes(self):
        closing = OrderIntent.close_position(
            "first",
            intent(),
            quantity="1",
            order_type="MARKET",
            decision_time=NOW,
            deadline_at=intent().deadline_at,
        )
        decision, store = await self.decide(
            [
                ("first", risk_order("first", filled=True)),
                ("second", risk_order("second", filled=True)),
            ],
            closing=closing,
        )
        self.assertTrue(decision.approved, decision.reason)
        self.assertEqual(store.orders["new-order"]["reservations"]["request-1"]["cost"], "0")
        decision, _ = await self.decide(
            [
                ("first", risk_order("first", filled=True, owner="other")),
            ],
            closing=closing,
        )
        self.assertEqual(decision.reason, "POSITION_OWNER_MISMATCH")

    async def test_working_close_reserves_lot_and_portfolio_hold_remains_enforced(self):
        closing = OrderIntent.close_position(
            "first",
            intent(),
            quantity="1",
            order_type="MARKET",
            decision_time=NOW,
            deadline_at=intent().deadline_at,
        )
        decision, _ = await self.decide(
            [
                ("first", risk_order("first", filled=True)),
                ("close-1", risk_order("close-1", closing, pending=True)),
            ],
            closing=closing,
        )
        self.assertEqual(decision.reason, "INSUFFICIENT_CLOSE_QUANTITY")
        decision, _ = await self.decide(
            [("first", risk_order("first", filled=True))],
            closing=closing,
            external_hold="PORTFOLIO_UNAVAILABLE",
        )
        self.assertEqual(decision.reason, "PORTFOLIO_UNAVAILABLE")


class MhiRecoveryScopeTests(unittest.TestCase):
    def test_owner_loss_recovery_covers_explicit_mhi_and_mes_scopes(self):
        config = approved_settings(paper_signal_only_allowlist={"mhi-vwap": ("MES2611",)})
        allowlist = getattr(config, "recovery_allowlist", {})
        recovery = Recovery(SimpleNamespace(settings=config, store=None), allowlist)
        self.assertTrue(recovery.scoped("mhi-vwap", intent()))
        self.assertTrue(recovery.scoped("mhi-vwap", intent("MES")))
        self.assertFalse(recovery.scoped("other", intent()))
        self.assertFalse(
            recovery.scoped("mhi-vwap", intent("HSI", order_type="LIMIT", limit_amount="25000"))
        )


if __name__ == "__main__":
    unittest.main()
