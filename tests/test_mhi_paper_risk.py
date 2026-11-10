import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from oms_protocol.messages import ProtocolError
from oms_protocol.orders import OrderIntent, OrderLeg
from pydantic import ValidationError

from portfolio_service.lots import build_lots, validate_close
from risk_service.config import Settings
from risk_service.policies import Engine, Usage

NOW = datetime(2026, 11, 10, 2, tzinfo=timezone.utc)
EXPIRY = date(2026, 11, 27)


def intent(symbol="MHI", **changes):
    values = dict(
        legs=(
            OrderLeg(
                instrument_id=f"{symbol}2611",
                asset_type="FUTURE",
                underlying=symbol,
                side="BUY",
                quantity_unit="CONTRACTS",
                expiry=EXPIRY,
                underlying_type="INDEX",
                settlement="CASH",
            ),
        ),
        quantity="1",
        currency="USD" if symbol == "MES" else "HKD",
        order_type="MARKET",
        price_unit="INDEX_POINTS",
        decision_time=NOW,
        deadline_at=NOW + timedelta(seconds=30),
    )
    return OrderIntent(**(values | changes))


def settings(**changes):
    values = dict(
        nats_password="test",
        mode="IB_PAPER",
        require_portfolio=True,
        strategies={"mhi-vwap": {}, "other": {}},
        instruments={
            f"{symbol}2611": dict(
                asset_type="FUTURE",
                underlying=symbol,
                expiry=EXPIRY,
                currency="USD" if symbol == "MES" else "HKD",
                multiplier=str(multiplier),
                future_contract_budget="500000",
                underlying_type="INDEX",
                settlement="CASH",
            )
            for symbol, multiplier in (("MHI", 10), ("HSI", 50), ("MES", 5))
        },
        account_by_currency={"HKD": dict(max_order_cost="500100", max_committed="1000200")},
        strategies_by_currency={
            key: {"HKD": dict(max_order_cost="500100", max_committed="1000200")}
            for key in ("mhi-vwap", "other")
        },
        fee_buffers_by_currency={"HKD": "100"},
    )
    return Settings(**(values | changes))


def approved_settings(**changes):
    return settings(**(dict(paper_market_allowlist={"mhi-vwap": ("MHI2611",)}) | changes))


class MhiPaperPolicyTests(unittest.TestCase):
    def evaluate(self, order=None, config=None, **changes):
        args = dict(
            intent=order or intent(),
            strategy_id="mhi-vwap",
            account=Usage(),
            strategy=Usage(),
            previous_cost=Decimal(0),
            now=NOW,
        )
        return Engine(config or approved_settings()).evaluate(**(args | changes))

    def test_market_protocol_admits_mhi_but_not_hsi_or_arbitrary_assets(self):
        try:
            self.assertEqual(intent().order_type, "MARKET")
        except ValidationError as error:
            self.fail(f"MHI MARKET should be a valid protocol intent: {error}")
        with self.assertRaises(ValidationError):
            intent("HSI")
        with self.assertRaises(ValidationError):
            intent(limit_amount="25000")
        with self.assertRaises(ValidationError):
            OrderIntent(
                legs=(
                    OrderLeg(
                        instrument_id="AAPL", asset_type="STOCK", side="BUY", quantity_unit="SHARES"
                    ),
                ),
                quantity="1",
                order_type="MARKET",
                decision_time=NOW,
                deadline_at=NOW + timedelta(seconds=30),
            )

    def test_market_authorization_is_explicit_and_strategy_scoped(self):
        self.assertEqual(self.evaluate(config=settings()).reason, "MARKET_NOT_AUTHORIZED")
        self.assertEqual(self.evaluate(strategy_id="other").reason, "MARKET_NOT_AUTHORIZED")

    def test_market_allowlist_requires_paper_portfolio_and_actual_mhi_catalog(self):
        for changes in (
            dict(mode="SIMULATION"),
            dict(require_portfolio=False),
            dict(paper_market_allowlist={"missing": ("MHI2611",)}),
            dict(paper_market_allowlist={"mhi-vwap": ("HSI2611",)}),
            dict(paper_market_allowlist={"mhi-vwap": ("MES2611",)}),
            dict(paper_market_allowlist={"mhi-vwap": ("unknown",)}),
            dict(paper_market_allowlist={"mhi-vwap": ()}),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                approved_settings(**changes)
        self.assertNotEqual(settings().policy_hash, approved_settings().policy_hash)

    def test_mhi_market_keeps_hkd_contract_budget_and_fees(self):
        assessment = self.evaluate()
        self.assertTrue(assessment.approved, assessment.reason)
        self.assertEqual(assessment.cost, Decimal("500100"))
        self.assertEqual(assessment.risk_basis, "FUTURES_CONTRACT_BUDGET")
        self.assertIsNone(assessment.expiry_max_loss)
        self.assertEqual(
            self.evaluate(account=Usage(committed=Decimal("500101"))).reason,
            "ACCOUNT_BUDGET_EXCEEDED",
        )
        self.assertEqual(
            self.evaluate(strategy=Usage(committed=Decimal("500101"))).reason,
            "STRATEGY_BUDGET_EXCEEDED",
        )
        self.assertEqual(
            self.evaluate(
                config=approved_settings(
                    account_by_currency={
                        "HKD": dict(max_order_cost="500000", max_committed="1000200")
                    }
                )
            ).reason,
            "ACCOUNT_ORDER_COST_EXCEEDED",
        )

    def test_mhi_market_requires_one_contract_and_disallows_replace(self):
        self.assertEqual(
            self.evaluate(order=intent(quantity="2")).reason, "MARKET_QUANTITY_OR_REPLACE"
        )
        self.assertEqual(self.evaluate(replacing=True).reason, "MARKET_QUANTITY_OR_REPLACE")

    def test_mhi_close_requires_ownership_and_bypasses_new_position_budget(self):
        opening = intent()
        closing = OrderIntent.close_position(
            "lot-1",
            opening,
            quantity="1",
            order_type="MARKET",
            decision_time=NOW,
            deadline_at=NOW + timedelta(seconds=30),
        )
        self.assertEqual(self.evaluate(order=closing).reason, "POSITION_VALIDATION_REQUIRED")
        result = self.evaluate(
            order=closing, reduce_only=True, account=Usage(committed=Decimal("9999999"))
        )
        self.assertTrue(result.approved, result.reason)
        self.assertEqual(result.cost, 0)
        views = [
            dict(
                order_id="lot-1",
                strategy_instance_id="mhi-vwap",
                intent=opening.model_dump(mode="json"),
                filled={"MHI2611": "1"},
                consistency="CONSISTENT",
            )
        ]
        lot = build_lots(views)["lot-1"]
        validate_close(closing, "mhi-vwap", lot)
        with self.assertRaisesRegex(ProtocolError, "POSITION_OWNER_MISMATCH"):
            validate_close(closing, "other", lot)

    def test_no_expiry_day_entries_but_owned_market_close_still_allowed(self):
        expiry_now = datetime(2026, 11, 27, 2, tzinfo=timezone.utc)
        opening = intent(decision_time=expiry_now, deadline_at=expiry_now + timedelta(seconds=30))
        self.assertEqual(
            self.evaluate(order=opening, now=expiry_now).reason, "FUTURE_EXPIRY_TOO_CLOSE"
        )
        closing = OrderIntent.close_position(
            "lot-1",
            opening,
            quantity="1",
            order_type="MARKET",
            decision_time=expiry_now,
            deadline_at=expiry_now + timedelta(seconds=30),
        )
        self.assertTrue(self.evaluate(order=closing, now=expiry_now, reduce_only=True).approved)

    def test_existing_mes_signal_only_behavior_is_unchanged(self):
        config = settings(paper_signal_only_allowlist={"mhi-vwap": ("MES2611",)})
        result = self.evaluate(
            order=intent("MES"), config=config, account=Usage(committed=Decimal("999999999"))
        )
        self.assertTrue(result.approved, result.reason)
        self.assertEqual((result.cost, result.risk_basis), (0, "PAPER_SIGNAL_ONLY"))
        self.assertEqual(
            self.evaluate(order=intent("MES", quantity="2"), config=config).reason,
            "SIGNAL_ONLY_QUANTITY_OR_REPLACE",
        )


if __name__ == "__main__":
    unittest.main()
