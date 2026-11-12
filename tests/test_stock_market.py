from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from oms_protocol.common import utcnow
from oms_protocol.orders import OrderIntent, ViewSnapshot
from portfolio_client.models import BrokerSnapshot
from pydantic import ValidationError

from portfolio_service.service import Service
from risk_service.config import Settings
from risk_service.policies import Engine, Usage

NOW = datetime(2026, 11, 12, 15, tzinfo=UTC)
STOCK = dict(
    instrument_id="XYZ",
    asset_type="STOCK",
    side="BUY",
    quantity_unit="SHARES",
    metadata_status="VERIFIED",
)


def intent(**changes):
    return OrderIntent.model_validate(
        dict(
            legs=[STOCK],
            quantity="10",
            order_type="MARKET",
            market_reference_price="100",
            market_reference_at=NOW - timedelta(seconds=1),
            market_budget_amount="1010",
            rth_only=True,
            decision_time=NOW,
            deadline_at=NOW + timedelta(seconds=2),
        )
        | changes
    )


class StockMarketTests(TestCase):
    def setUp(self):
        self.settings = Settings(
            mode="IB_PAPER",
            require_portfolio=True,
            nats_password="fixture",
            strategies={"alpha": {}, "beta": {}, "mes": {}},
            instruments={
                "XYZ": {"asset_type": "STOCK", "multiplier": "1"},
                "MES": {
                    "asset_type": "FUTURE",
                    "underlying": "MES",
                    "multiplier": "5",
                    "expiry": "2026-12-18",
                    "underlying_type": "INDEX",
                    "settlement": "CASH",
                },
            },
            paper_signal_only_allowlist={"mes": ("MES",)},
            paper_stock_market_allowlist={"alpha": ("XYZ",)},
        )

    def assess(self, order, strategy="alpha", **options):
        return Engine(self.settings).evaluate(
            order, strategy, Usage(), Usage(), Decimal(0), NOW, **options
        )

    def test_authorized_buy_reserves_buffered_notional_and_fees_once(self):
        order = intent()
        self.assertEqual(OrderIntent.model_validate_json(order.model_dump_json()), order)
        self.assertEqual(self.assess(order).cost, Decimal("1020"))
        self.assertEqual(self.assess(order, "beta").reason, "MARKET_NOT_AUTHORIZED")

    def test_close_requires_owned_lot_validation_and_uses_no_opening_reservation(self):
        order = intent(
            legs=[STOCK | dict(side="SELL", position_effect="CLOSE")],
            close_position_id="owned-lot",
            net_price_effect="CREDIT",
            market_budget_amount="1000",
        )
        self.assertEqual(self.assess(order).reason, "POSITION_VALIDATION_REQUIRED")
        result = self.assess(order, reduce_only=True)
        self.assertTrue(result.approved)
        self.assertEqual(result.cost, 0)

    def test_stock_market_requires_fresh_covered_long_day_order(self):
        for invalid in (
            {"market_reference_price": None},
            {"market_budget_amount": "999.99"},
            {"rth_only": False},
            {"time_in_force": "GTC"},
            {"market_reference_at": NOW - timedelta(seconds=10)},
            {"legs": [STOCK | {"metadata_status": "PROVISIONAL"}]},
            {"legs": [STOCK | {"side": "SELL"}], "net_price_effect": "CREDIT"},
            {"legs": [STOCK | {"position_effect": "CLOSE"}], "close_position_id": "short-lot"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                intent(**invalid)

    def test_legacy_mes_wire_and_market_authorization(self):
        raw = {
            "structure": "SINGLE",
            "legs": [
                {
                    "instrument_id": "MES",
                    "instrument_version": 1,
                    "asset_type": "FUTURE",
                    "side": "BUY",
                    "ratio": 1,
                    "position_effect": "OPEN",
                    "quantity_unit": "CONTRACTS",
                    "metadata_status": "VERIFIED",
                    "underlying": "MES",
                    "expiry": "2026-12-18",
                    "right": None,
                    "strike": None,
                    "underlying_type": "INDEX",
                    "settlement": "CASH",
                    "exercise_style": "AMERICAN",
                }
            ],
            "quantity": "1",
            "order_type": "MARKET",
            "limit_amount": None,
            "currency": "USD",
            "price_unit": "INDEX_POINTS",
            "net_price_effect": "DEBIT",
            "time_in_force": "DAY",
            "decision_time": "2026-11-12T15:00:00Z",
            "deadline_at": "2026-11-12T15:00:02Z",
            "quote_references": [],
        }
        order = OrderIntent.model_validate(raw)
        self.assertEqual(order.model_dump(mode="json"), raw)
        self.assertEqual(self.assess(order, "mes").risk_basis, "PAPER_SIGNAL_ONLY")
        self.assertEqual(self.assess(order).reason, "MARKET_NOT_AUTHORIZED")
        with self.assertRaises(ValidationError):
            OrderIntent.model_validate(raw | {"market_reference_price": "100"})


class BrokerStore:
    def __init__(self, snapshot):
        self.metadata = {
            "broker": {"snapshot": snapshot.model_dump(mode="json"), "ledger_revision": 0},
            "feed_epoch": "epoch",
        }

    @asynccontextmanager
    async def transaction(self):
        yield

    async def all(self, *args):
        return []

    async def meta(self, key, default=None):
        return self.metadata.get(key, default)


class CashProjectionTests(IsolatedAsyncioTestCase):
    async def test_projects_only_fresh_matched_actual_usd_ledger_cash(self):
        now = utcnow()
        snapshot = BrokerSnapshot(
            account_id="test",
            environment="paper",
            mode="IB_PAPER",
            feed_epoch="epoch",
            snapshot_id="snap",
            source_session="session",
            started_at=now - timedelta(seconds=1),
            completed_at=now,
            complete=True,
            stable=True,
            positions=(),
            catalog=(),
            account_values=(
                {"tag": "$LEDGER-CashBalance", "currency": "USD", "value": "102345.67"},
                {"tag": "$LEDGER-TotalCashBalance", "currency": "USD", "value": "102346.00"},
                {"tag": "TotalCashValue", "currency": "USD", "value": "190000"},
                {"tag": "AvailableFunds", "currency": "USD", "value": "900000"},
            ),
        )
        settings = SimpleNamespace(
            strategies=("alpha",),
            max_positions=10,
            max_snapshot_age=10,
            poll_interval=1,
            rpc_timeout=5,
            mode="IB_PAPER",
            account_id="test",
        )
        store = BrokerStore(snapshot)
        service = Service(settings, store)

        async def cash():
            result = await service.snapshot("alpha")
            return ViewSnapshot.model_validate(result["snapshot"]).usd_cash

        value = await cash()
        self.assertEqual(value.amount, Decimal("102345.67"))
        self.assertEqual(
            (value.started_at, value.completed_at, value.snapshot_id),
            (snapshot.started_at, now, "snap"),
        )
        raw = store.metadata["broker"]["snapshot"]
        for change in (
            {"started_at": (now - timedelta(seconds=20)).isoformat()},
            {"stable": False},
            {"account_values": raw["account_values"][2:]},
        ):
            with self.subTest(change=change):
                store.metadata["broker"]["snapshot"] = raw | change
                self.assertIsNone(await cash())
