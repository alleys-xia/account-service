import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from oms_protocol.common import digest
from oms_protocol.messages import ProtocolError, RiskDecision, RiskRequest
from oms_protocol.orders import OrderIntent, OrderLeg
from pydantic import SecretStr, ValidationError

from account_runtime.app import validate_domains
from oms_service.domain import Order, validate_replacement
from oms_service.recovery import Recovery
from portfolio_service.config import Instrument as PortfolioInstrument
from portfolio_service.ledger import fold, record
from risk_service.config import Instrument, Settings
from risk_service.ledger import empty_order, project
from risk_service.policies import Engine, Usage
from risk_service.service import Service
from tests.test_mhi_admission import RiskStore
from tests.test_mhi_paper_risk import intent as legacy_intent
from tests.test_mhi_paper_risk import settings as legacy_settings

NOW = datetime(2026, 11, 11, 2, tzinfo=timezone.utc)
EXPIRY = date(2026, 12, 18)
# Independent economic fixtures: dollars per IB price unit, and IB quote tick.
PRODUCTS = {
    "NES": ("INDEX", "CASH", "0.5", "0.5", "6000"),
    "MTN": ("RATE", "CASH", "100", "0.015625", "115"),
    "MNG": ("COMMODITY", "CASH", "1000", "0.001", "3.65"),
    "1OZ": ("COMMODITY", "CASH", "1", "0.25", "4000"),
    "MZC": ("COMMODITY", "CASH", "5", "0.5", "450"),
    "M6E": ("CURRENCY", "PHYSICAL", "12500", "0.0001", "1.1"),
}


def intent(root="NES", **changes):
    kind, settlement, _, _, price = PRODUCTS[root]
    values = dict(
        legs=(
            OrderLeg(
                instrument_id=root + "2612",
                asset_type="FUTURE",
                underlying=root,
                expiry=EXPIRY,
                underlying_type=kind,
                settlement=settlement,
                side="BUY",
                quantity_unit="CONTRACTS",
                metadata_status="VERIFIED",
            ),
        ),
        quantity="1",
        order_type="LIMIT",
        limit_amount=price,
        price_unit="INDEX_POINTS",
        decision_time=NOW,
        deadline_at=NOW + timedelta(seconds=30),
    )
    return OrderIntent(**(values | changes))


def stop(root="NES", **changes):
    return OrderIntent.close_position(
        "lot-1",
        intent(root),
        **(
            dict(
                quantity="1",
                order_type="STOP",
                stop_amount=PRODUCTS[root][4],
                time_in_force="GTC",
                decision_time=NOW,
                deadline_at=NOW + timedelta(seconds=30),
            )
            | changes
        ),
    )


def settings(**changes):
    values = dict(
        nats_password="test",
        mode="IB_PAPER",
        require_portfolio=True,
        account=dict(max_order_cost="20000", max_committed="40000", max_future_quantity=2),
        strategies={
            "all-weather-trend": dict(
                max_order_cost="20000",
                max_committed="40000",
                max_future_quantity=2,
            )
        },
        paper_market_allowlist={"all-weather-trend": tuple(root + "2612" for root in PRODUCTS)},
        instruments={
            root + "2612": dict(
                asset_type="FUTURE",
                underlying=root,
                expiry=EXPIRY,
                underlying_type=kind,
                settlement=settlement,
                multiplier=factor,
                price_increment=tick,
                future_contract_budget="1000",
                entry_cutoff_at="2026-12-10T14:00:00Z",
            )
            for root, (kind, settlement, factor, tick, _) in PRODUCTS.items()
        },
    )
    return Settings(**(values | changes))


def risk_order(identity, order_intent, *, filled="0", active=False, pending=None):
    view = Order(
        order_id=identity,
        client_order_id=identity,
        strategy_instance_id="all-weather-trend",
        account_id="default",
        intent=order_intent,
        command_id=identity,
        session_token="token",
        session_epoch=1,
        revision=1,
        status="FILLED" if Decimal(filled) == order_intent.quantity else "SUBMITTED",
        dispatch_certainty="ACKNOWLEDGED",
        filled={order_intent.legs[0].instrument_id: filled},
        pending=pending,
    ).view()
    data = empty_order() | dict(view=view, ever_filled=Decimal(filled) > 0)
    data["reservations"] = {
        identity: dict(
            revision=1,
            cost="1001",
            quantity=str(order_intent.quantity),
            fee_buffer="100",
            active=active,
            definitely_not_executed=False,
        )
    }
    if Decimal(filled):
        data["executions"][identity] = dict(
            leg_id=order_intent.legs[0].instrument_id,
            quantity=filled,
            price="6000",
            fees="0",
            superseded=False,
        )
    return data


class FuturesProtocolTests(unittest.TestCase):
    def test_supported_roots_keep_true_economic_classification(self):
        for root, (kind, settlement, _, _, _) in PRODUCTS.items():
            with self.subTest(root=root):
                try:
                    value = intent(root, order_type="MARKET", limit_amount=None)
                except ValidationError as error:
                    self.fail(f"Approved {root} must be representable: {error}")
                self.assertEqual(value.legs[0].underlying_type, kind)
                self.assertEqual(value.legs[0].settlement, settlement)
                data = value.model_dump(mode="json")
                data["legs"][0]["underlying"] = "UNVERIFIED"
                with self.assertRaises(ValidationError):
                    OrderIntent.model_validate(data)

    def test_gtc_stop_is_close_only_and_has_one_explicit_trigger(self):
        try:
            value = stop()
        except ValidationError as error:
            self.fail(f"Native close STOP must be representable: {error}")
        self.assertEqual(value.stop_amount, Decimal("6000"))
        self.assertEqual(value.legs[0].side, "SELL")
        for changes in (
            dict(stop_amount=None),
            dict(stop_amount="0"),
            dict(stop_amount="NaN"),
            dict(limit_amount="6000"),
            dict(time_in_force="DAY"),
            dict(order_type="MARKET"),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                stop(**changes)
        with self.assertRaises(ValidationError):
            intent(order_type="STOP", limit_amount=None, stop_amount="5900", time_in_force="GTC")

    def test_existing_intent_canonical_hash_is_unchanged(self):
        value = legacy_intent()
        self.assertNotIn("stop_amount", value.model_dump(mode="json"))
        self.assertEqual(
            digest(value.model_dump(mode="json")),
            "c24ffb6e943d498828dbbce771ed3bba8f1c8f0ae3e30add544b5865ffdee90e",
        )

    def test_approved_stop_requires_and_serializes_exact_stop_bound(self):
        fields = dict(
            account_id="default",
            event_id="event",
            request_id="request",
            order_id="order",
            order_revision=1,
            intent_hash="hash",
            approved=True,
            reservation_id="reserved",
            approved_quantity="1",
            valid_until=NOW + timedelta(seconds=5),
            applied_account_seq=0,
        )
        try:
            approved = RiskDecision(**fields, order_type="STOP", approved_stop="6000")
        except ValidationError as error:
            self.fail(f"STOP approval needs a signed stop bound: {error}")
        self.assertEqual(approved.model_dump(mode="json")["approved_stop"], "6000")
        with self.assertRaises(ValidationError):
            RiskDecision(**fields, order_type="STOP")
        with self.assertRaises(ValidationError):
            RiskDecision(**fields, order_type="MARKET", approved_stop="6000")
        self.assertNotIn(
            "approved_stop",
            RiskDecision(
                **fields,
                order_type="LIMIT",
                approved_limit="6000",
            ).model_dump(mode="json"),
        )


class FuturesPolicyTests(unittest.TestCase):
    def evaluate(self, order, config=None, **changes):
        return Engine(config or settings()).evaluate(
            **(
                dict(
                    intent=order,
                    strategy_id="all-weather-trend",
                    account=Usage(),
                    strategy=Usage(),
                    previous_cost=Decimal(0),
                    now=NOW,
                    reduce_only=bool(order.close_position_id),
                )
                | changes
            )
        )

    def test_contract_budgets_and_ticks_for_each_approved_product(self):
        for root in PRODUCTS:
            with self.subTest(root=root):
                try:
                    config = settings()
                except ValidationError as error:
                    self.fail(f"New explicit futures catalogs must load: {error}")
                result = self.evaluate(intent(root), config)
                self.assertTrue(result.approved, result.reason)
                self.assertEqual(result.cost, Decimal("1001"))
                self.assertEqual(result.risk_basis, "FUTURES_CONTRACT_BUDGET")
                bad = intent(
                    root, limit_amount=str(Decimal(PRODUCTS[root][4]) + Decimal("0.00001"))
                )
                self.assertEqual(self.evaluate(bad).reason, "INVALID_FUTURE_TICK")

    def test_existing_mes_expiry_gate_keeps_new_york_calendar_day(self):
        now = datetime(2026, 11, 27, 5, 30, tzinfo=timezone.utc)
        order = legacy_intent("MES", decision_time=now, deadline_at=now + timedelta(seconds=30))
        config = legacy_settings(paper_signal_only_allowlist={"mhi-vwap": ("MES2611",)})
        result = Engine(config).evaluate(order, "mhi-vwap", Usage(), Usage(), Decimal(0), now)
        self.assertEqual(result.reason, "FUTURE_EXPIRY_TOO_CLOSE")

    def test_new_roots_require_explicit_strategy_scope_and_valid_metadata(self):
        config = settings()
        for root in PRODUCTS:
            self.assertEqual(
                self.evaluate(
                    intent(root), config.model_copy(update={"paper_market_allowlist": {}})
                ).reason,
                "FUTURE_NOT_AUTHORIZED",
            )
        corn = config.instruments["MZC2612"].model_dump()
        for changes in (
            dict(multiplier="500"),
            dict(price_increment="0.25"),
            dict(underlying_type="INDEX"),
            dict(future_contract_budget=None),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Instrument.model_validate(corn | changes)

    def test_m6e_cannot_enter_without_cutoff_or_at_cutoff_but_can_exit(self):
        config = settings()
        reference = config.instruments["M6E2612"]
        for cutoff, reason in (
            (None, "FUTURE_ENTRY_CUTOFF_REQUIRED"),
            (NOW, "FUTURE_ENTRY_CUTOFF_REACHED"),
        ):
            current = config.model_copy(
                update={
                    "instruments": config.instruments
                    | {"M6E2612": reference.model_copy(update={"entry_cutoff_at": cutoff})}
                }
            )
            self.assertEqual(self.evaluate(intent("M6E"), current).reason, reason)
            self.assertTrue(self.evaluate(stop("M6E"), current).approved)

    def test_stop_admission_is_reduce_only_and_tick_checked(self):
        result = self.evaluate(stop())
        self.assertTrue(result.approved, result.reason)
        self.assertEqual(result.cost, 0)
        self.assertEqual(
            self.evaluate(stop(), reduce_only=False).reason, "POSITION_VALIDATION_REQUIRED"
        )
        self.assertEqual(self.evaluate(stop(stop_amount="6000.1")).reason, "INVALID_FUTURE_TICK")

    def test_stop_replacement_may_tighten_but_never_loosen_or_change_order_type(self):
        value = stop()
        validate_replacement(value, stop(stop_amount="6001"))
        for changed in (
            stop(stop_amount="5999"),
            OrderIntent.close_position(
                "lot-1",
                intent(),
                quantity="1",
                order_type="MARKET",
                decision_time=NOW,
                deadline_at=NOW + timedelta(seconds=30),
            ),
        ):
            with self.assertRaises(ProtocolError):
                validate_replacement(value, changed)

    def test_short_stop_tightens_downwards(self):
        opening = intent(
            legs=(intent().legs[0].model_copy(update={"side": "SELL"}),), net_price_effect="CREDIT"
        )
        fields = dict(
            quantity="1",
            order_type="STOP",
            time_in_force="GTC",
            decision_time=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        )
        old = OrderIntent.close_position("lot-1", opening, stop_amount="6100", **fields)
        validate_replacement(
            old,
            OrderIntent.close_position(
                "lot-1",
                opening,
                stop_amount="6099",
                **fields,
            ),
        )
        with self.assertRaisesRegex(ProtocolError, "STOP_WOULD_LOOSEN"):
            validate_replacement(
                old,
                OrderIntent.close_position(
                    "lot-1",
                    opening,
                    stop_amount="6101",
                    **fields,
                ),
            )


class FuturesLedgerTests(unittest.TestCase):
    def test_account_startup_rejects_missing_new_product_identity_even_if_multiplier_matches(self):
        risk = settings()
        portfolio = SimpleNamespace(
            **{
                name: getattr(risk, name)
                for name in (
                    "environment",
                    "account_id",
                    "mode",
                    "nats_url",
                    "strategies",
                )
            },
            database_url=SecretStr("postgresql://localhost/portfolio_dev"),
            instruments={
                name: PortfolioInstrument(
                    asset_type="FUTURE",
                    underlying=ref.underlying,
                    multiplier=ref.multiplier,
                )
                for name, ref in risk.instruments.items()
            },
        )
        oms = SimpleNamespace(
            **{
                name: getattr(risk, name)
                for name in (
                    "environment",
                    "account_id",
                    "mode",
                    "nats_url",
                    "strategies",
                )
            },
            database_url=SecretStr("postgresql://localhost/oms_dev"),
        )
        validate_domains([oms, risk, portfolio])
        portfolio.instruments["MZC2612"] = PortfolioInstrument(asset_type="FUTURE", multiplier="5")
        with self.assertRaisesRegex(ProtocolError, "ACCOUNT_MODULE_CATALOG_MISMATCH"):
            validate_domains([oms, risk, portfolio])

    def test_each_quote_unit_produces_correct_realized_dollars(self):
        for root, (_, _, factor, tick, price) in PRODUCTS.items():
            with self.subTest(root=root):
                try:
                    reference = PortfolioInstrument(
                        asset_type="FUTURE", underlying=root, multiplier=factor
                    )
                except ValidationError as error:
                    self.fail(f"Portfolio must recognize {root}: {error}")
                opening = intent(root)
                closing = stop(root)
                trades = []
                for seq, order, execution_price in (
                    (1, opening, price),
                    (2, closing, str(Decimal(price) + Decimal(tick))),
                ):
                    trades.append(
                        record(
                            dict(
                                leg_id=root + "2612",
                                quantity="1",
                                price=execution_price,
                                fees="0",
                            ),
                            dict(
                                order_id="lot-1" if seq == 1 else "stop-1",
                                strategy_instance_id="all-weather-trend",
                                intent=order.model_dump(mode="json"),
                            ),
                            seq,
                            reference,
                        )
                    )
                result = fold(trades)
                expected = {
                    "NES": "0.25",
                    "MTN": "1.5625",
                    "MNG": "1",
                    "1OZ": "0.25",
                    "MZC": "2.5",
                    "M6E": "1.25",
                }[root]
                self.assertEqual(Decimal(result["realized_pnl_gross"]), Decimal(expected))
                self.assertEqual(Decimal(result["quantity"]), 0)


class StopReservationTests(unittest.IsolatedAsyncioTestCase):
    async def decide(self, order, others, *, operation="submit"):
        pending = dict(
            operation=operation, revision=2 if operation == "replace" else 1, request_id="request-1"
        )
        if operation == "replace":
            current = others.pop("new-order")
            current["view"].update(
                pending_operation="replace",
                pending_intent_revision=2,
                pending_request_id="request-1",
            )
        else:
            current = risk_order("new-order", order, pending=pending)
            current["reservations"] = {}
        store = RiskStore(others | {"new-order": current})
        request = RiskRequest(
            account_id="default",
            feed_epoch="feed-1",
            message_id="request-1",
            order_id="new-order",
            strategy_instance_id="all-weather-trend",
            order_revision=pending["revision"],
            intent=order,
            intent_hash=digest(order.model_dump(mode="json")),
            required_account_seq=0,
            operation=operation,
            mode="IB_PAPER",
        )
        with patch("risk_service.service.utcnow", return_value=NOW):
            decision = await Service(settings(), store).decide(request)
        return decision, store

    async def test_stop_and_market_close_are_mutually_exclusive_until_confirmed_cancel(self):
        opening = risk_order("lot-1", intent(), filled="1")
        protective = risk_order("stop-1", stop(), active=True)
        market = OrderIntent.close_position(
            "lot-1",
            intent(),
            quantity="1",
            order_type="MARKET",
            decision_time=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        )
        decision, _ = await self.decide(market, {"lot-1": opening, "stop-1": protective})
        self.assertEqual(decision.reason, "INSUFFICIENT_CLOSE_QUANTITY")
        protective["view"]["cancel_state"] = "REQUESTED"
        decision, _ = await self.decide(market, {"lot-1": opening, "stop-1": protective})
        self.assertEqual(decision.reason, "INSUFFICIENT_CLOSE_QUANTITY")
        cancelled = protective["view"] | dict(status="CANCELLED", cancel_state="CONFIRMED")
        self.assertIsNone(
            project(protective, dict(kind="order_update", status="CANCELLED", state=cancelled))
        )
        decision, _ = await self.decide(market, {"lot-1": opening, "stop-1": protective})
        self.assertTrue(decision.approved, decision.reason)

    async def test_protection_and_tightening_reserve_existing_lot_once(self):
        opening = risk_order("lot-1", intent(), filled="1")
        decision, _ = await self.decide(stop(), {"lot-1": opening})
        self.assertTrue(decision.approved, decision.reason)
        self.assertEqual(decision.approved_stop, Decimal("6000"))
        current = risk_order("new-order", stop(), active=True)
        decision, _ = await self.decide(
            stop(stop_amount="6001"),
            {
                "lot-1": opening,
                "new-order": current,
            },
            operation="replace",
        )
        self.assertTrue(decision.approved, decision.reason)

    async def test_pending_and_filled_openings_count_towards_root_caps(self):
        for root, quantity in (("NES", "1"), ("MZC", "2")):
            order = intent(root, quantity=quantity, order_type="MARKET", limit_amount=None)
            existing = risk_order("lot-1", order, active=True)
            decision, _ = await self.decide(order, {"lot-1": existing})
            self.assertEqual(decision.reason, "MARKET_POSITION_LIMIT")

    async def test_cancelled_partial_stop_releases_only_the_remaining_lot(self):
        opening = risk_order("lot-1", intent("MZC", quantity="2"), filled="2")
        protective = risk_order("stop-1", stop("MZC", quantity="2"), filled="1", active=True)
        cancelled = protective["view"] | dict(status="CANCELLED", cancel_state="CONFIRMED")
        self.assertIsNone(
            project(protective, dict(kind="order_update", status="CANCELLED", state=cancelled))
        )
        closing = OrderIntent.close_position(
            "lot-1",
            intent("MZC", quantity="2"),
            quantity="2",
            order_type="MARKET",
            decision_time=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        )
        others = {"lot-1": opening, "stop-1": protective}
        decision, _ = await self.decide(closing, others)
        self.assertEqual(decision.reason, "INSUFFICIENT_CLOSE_QUANTITY")
        decision, _ = await self.decide(closing.model_copy(update={"quantity": Decimal(1)}), others)
        self.assertTrue(decision.approved, decision.reason)

    async def test_tightened_stop_projection_keeps_reservation_and_rejects_looser_replace(self):
        opening = risk_order("lot-1", intent(), filled="1")
        current = risk_order("new-order", stop(), active=True)
        decision, _ = await self.decide(
            stop(stop_amount="5999"),
            {
                "lot-1": opening,
                "new-order": current,
            },
            operation="replace",
        )
        self.assertEqual(decision.reason, "INVALID_REPLACEMENT")
        updated = current["view"] | dict(intent=stop(stop_amount="6001").model_dump(mode="json"))
        self.assertIsNone(
            project(current, dict(kind="order_update", status="REPLACED", state=updated))
        )
        self.assertTrue(current["reservations"]["new-order"]["active"])


class RecoveryStore:
    def __init__(self, value, orders):
        self.value, self.orders = value, orders
        self.feed_epoch = "feed-1"
        self.db = self

    @asynccontextmanager
    async def transaction(self):
        yield

    async def one(self, sql, params=()):
        if "oms.strategy_recoveries" in sql:
            return dict(data=self.value)
        if "oms.sessions" in sql:
            return dict(data=self.value["lease"])
        raise AssertionError(sql)

    async def all(self, sql, params=()):
        if "oms.orders" in sql:
            return [dict(data=o.model_dump(mode="json")) for o in self.orders]
        raise AssertionError(sql)

    async def execute(self, sql, params):
        if sql.startswith("INSERT INTO oms.strategy_recoveries"):
            self.value = params[1].obj


class StopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_loss_cancels_protective_stop_before_any_market_close(self):
        config = settings()
        value = dict(
            recovery_id="recovery-1",
            state="CLEANING",
            reason="OWNER_SESSION_EXPIRED",
            instruments=list(config.paper_market_allowlist["all-weather-trend"]),
            feed_epoch="feed-1",
            pending_command=None,
            attempt=0,
            next_action_at=NOW.isoformat(),
            lease=dict(
                run_id="recovery",
                token="token",
                epoch=2,
                expires_at=(NOW + timedelta(seconds=30)).isoformat(),
            ),
        )
        order = Order(
            order_id="stop-1",
            client_order_id="stop-1",
            strategy_instance_id="all-weather-trend",
            account_id="default",
            intent=stop(),
            command_id="stop-1",
            session_token="token",
            session_epoch=1,
            status="SUBMITTED",
            dispatch_certainty="ACKNOWLEDGED",
        )
        store = RecoveryStore(value, [order])
        recovery_settings = SimpleNamespace(
            mode="IB_PAPER",
            strategies=config.strategies,
            session_ttl=30,
            account_id="default",
            environment="paper",
        )
        recovery = Recovery(
            SimpleNamespace(settings=recovery_settings, store=store), config.recovery_allowlist
        )
        self.assertTrue(recovery.scoped("all-weather-trend", stop()))
        with patch("oms_service.recovery.utcnow", return_value=NOW):
            result = await recovery.prepare("all-weather-trend", None)
        self.assertEqual((result.operation, result.order_id), ("cancel", "stop-1"))


if __name__ == "__main__":
    unittest.main()
