"""Pure, extensible policy chain. Storage/admission atomicity cannot be overridden by a rule."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from oms_protocol.futures import (
    FUTURE_POSITION_CAPS,
    FUTURES,
    RESTRICTED_FUTURES,
    entry_cutoff_reason,
)
from oms_protocol.orders import STOCK_SHORT_BUDGET_FACTOR, OrderIntent

from .config import Settings

ZERO = Decimal("0")


@dataclass(frozen=True)
class Usage:
    committed: Decimal = ZERO
    open_orders: int = 0
    requests_last_minute: int = 0
    pending: Decimal = ZERO
    position: Decimal = ZERO


@dataclass(frozen=True)
class Context:
    intent: OrderIntent
    strategy_id: str
    cost: Decimal
    previous_cost: Decimal
    account: Usage
    strategy: Usage
    now: datetime


class Rule(Protocol):
    """Return None to pass, or a stable public reason code to reject. Never do I/O here."""

    def check(self, context: Context) -> str | None: ...


@dataclass(frozen=True)
class Assessment:
    approved: bool
    reason: str = ""
    cost: Decimal = ZERO
    expiry_max_loss: Decimal | None = ZERO
    risk_basis: str = "EXPIRY_MAX_LOSS"


class Engine:
    def __init__(self, settings: Settings, *, extra_rules: tuple[Rule, ...] = ()):
        self.settings, self.extra_rules = settings, extra_rules

    def evaluate(
        self,
        intent,
        strategy_id,
        account,
        strategy,
        previous_cost,
        now,
        *,
        reduce_only=False,
        replacing=False,
    ):
        s = self.settings

        def reject(reason):
            return Assessment(False, reason)

        if strategy_id not in s.strategies:
            return reject("STRATEGY_NOT_AUTHORIZED")
        if intent.deadline_at <= now:
            return reject("REQUEST_EXPIRED")
        age = (now - intent.decision_time).total_seconds()
        if age < -2 or age > s.max_decision_age:
            return reject("STALE_DECISION")
        currency = intent.currency
        future = intent.legs[0].asset_type == "FUTURE"
        stock_short = intent.is_stock_short_open
        if currency == "USD":
            account_limits, strategy_limits = s.account, s.strategies[strategy_id]
            fee_buffer = s.fee_buffer_per_unit_per_leg
        else:
            account_limits = s.account_by_currency.get(currency)
            strategy_limits = s.strategies_by_currency.get(strategy_id, {}).get(currency)
            fee_buffer = s.fee_buffers_by_currency.get(currency)
            if account_limits is None or strategy_limits is None or fee_buffer is None:
                return reject("CURRENCY_POLICY_REQUIRED")
        closing = intent.close_position_id is not None
        if closing != reduce_only:
            return reject("POSITION_VALIDATION_REQUIRED")
        allowed_mes = s.paper_signal_only_allowlist.get(strategy_id, ())
        signal_only = s.mode == "IB_PAPER" and intent.legs[0].instrument_id in allowed_mes
        allowed_mhi = s.paper_market_allowlist.get(strategy_id, ())
        paper_market = s.mode == "IB_PAPER" and intent.legs[0].instrument_id in allowed_mhi
        restricted_future = future and intent.legs[0].underlying in RESTRICTED_FUTURES
        if restricted_future and not paper_market:
            return reject("FUTURE_NOT_AUTHORIZED")
        if intent.order_type == "STOP" and not (restricted_future and paper_market):
            return reject("STOP_NOT_AUTHORIZED")
        if signal_only and intent.order_type != "MARKET":
            return reject("SIGNAL_ONLY_MARKET_REQUIRED")
        stock_market = intent.is_stock_market
        if stock_market:
            allowed_stocks = s.paper_stock_market_allowlist.get(strategy_id, ())
            if s.mode != "IB_PAPER" or intent.legs[0].instrument_id not in allowed_stocks:
                return reject("MARKET_NOT_AUTHORIZED")
            if replacing:
                return reject("MARKET_QUANTITY_OR_REPLACE")
        elif intent.order_type == "MARKET" and not (signal_only or paper_market):
            return reject("MARKET_NOT_AUTHORIZED")
        if signal_only and (intent.quantity != 1 or replacing):
            return reject("SIGNAL_ONLY_QUANTITY_OR_REPLACE")
        market_cap = FUTURE_POSITION_CAPS.get(intent.legs[0].underlying, 1)
        if (
            paper_market
            and intent.order_type == "MARKET"
            and (intent.quantity > market_cap or replacing)
        ):
            return reject("MARKET_QUANTITY_OR_REPLACE")
        for leg in intent.legs:
            if not closing and (
                intent.structure == "SINGLE"
                and leg.side != "BUY"
                and leg.asset_type not in {"STOCK", "FUTURE"}
            ):
                return reject("POSITION_SUPPORT_REQUIRED")
            if stock_short and leg.instrument_id not in s.stock_short_allowlist.get(
                strategy_id, ()
            ):
                return reject("STOCK_SHORT_NOT_AUTHORIZED")
            reference = s.instruments.get(leg.instrument_id)
            if reference is None:
                return reject("UNKNOWN_INSTRUMENT")
            if (
                reference.currency != currency
                or reference.asset_type != leg.asset_type
                or reference.version != leg.instrument_version
            ):
                return reject("INSTRUMENT_MISMATCH")
            if leg.asset_type == "FUTURE":
                if any(
                    getattr(leg, name) != getattr(reference, name)
                    for name in ("underlying", "expiry", "underlying_type", "settlement")
                ):
                    return reject("INSTRUMENT_MISMATCH")
                zone = (
                    "Asia/Hong_Kong"
                    if reference.currency == "HKD"
                    else "America/New_York"
                    if reference.underlying == "MES"
                    else "America/Chicago"
                )
                dte = (reference.expiry - now.astimezone(ZoneInfo(zone)).date()).days
                if dte < (0 if closing else s.min_future_dte):
                    return reject("FUTURE_EXPIRY_TOO_CLOSE")
                if not closing:
                    reason = entry_cutoff_reason(
                        reference.underlying, reference.entry_cutoff_at, now
                    )
                    if reason:
                        return reject(reason)
                tick = FUTURES[reference.underlying].tick
                price = intent.stop_amount if intent.order_type == "STOP" else intent.limit_amount
                if price is not None and price % tick:
                    return reject("INVALID_FUTURE_TICK")
            if leg.asset_type == "OPTION":
                if any(
                    getattr(leg, name) != getattr(reference, name)
                    for name in (
                        "underlying",
                        "expiry",
                        "right",
                        "strike",
                        "underlying_type",
                        "settlement",
                        "exercise_style",
                    )
                ):
                    return reject("INSTRUMENT_MISMATCH")
                if (reference.expiry - now.astimezone(ZoneInfo("America/New_York")).date()).days < (
                    0 if closing else s.min_option_dte
                ):
                    return reject("OPTION_EXPIRY_TOO_CLOSE")
        multiplier = s.instruments[intent.legs[0].instrument_id].multiplier
        if signal_only:
            base = expiry_loss = ZERO
            fee_buffer = ZERO
        elif stock_market:
            base = intent.market_budget_amount
            expiry_loss = None
        elif intent.structure == "SINGLE":
            if intent.net_price_effect != (
                ("DEBIT" if intent.legs[0].side == "BUY" else "CREDIT")
                if future or intent.legs[0].asset_type == "STOCK"
                else ("CREDIT" if closing else "DEBIT")
            ) or (intent.order_type == "LIMIT" and intent.limit_amount <= 0):
                return reject("INVALID_DEBIT")
            unit_budget = (
                s.instruments[intent.legs[0].instrument_id].future_contract_budget
                if future
                else s.instruments[intent.legs[0].instrument_id].stock_short_budget_per_share
                if stock_short
                else intent.limit_amount * multiplier
            )
            if unit_budget is None:
                return reject("FUTURE_BUDGET_REQUIRED")
            base = expiry_loss = unit_budget * intent.quantity
            if (
                stock_short
                and intent.limit_amount * STOCK_SHORT_BUDGET_FACTOR * intent.quantity > base
            ):
                return reject("STOCK_SHORT_BUDGET_EXCEEDED")
        else:
            width = abs(intent.legs[0].strike - intent.legs[1].strike)
            if not ZERO < intent.limit_amount <= width or (
                not closing and intent.limit_amount == width
            ):
                return reject("INVALID_CLOSE_DEBIT" if closing else "INVALID_CREDIT")
            base = width * multiplier * intent.quantity
            expiry_loss = (width - intent.limit_amount) * multiplier * intent.quantity
        cost = base + fee_buffer * len(intent.legs) * intent.quantity
        order_cost = cost
        if closing:
            cost = expiry_loss = ZERO
        if future or stock_short:
            expiry_loss = None
        incremental = max(ZERO, cost - previous_cost)
        for name, limits, usage in (
            ("ACCOUNT", account_limits, account),
            ("STRATEGY", strategy_limits, strategy),
        ):
            global_limits = s.account if name == "ACCOUNT" else s.strategies[strategy_id]
            if (global_limits.halted or limits.halted) and not closing:
                return reject(name + "_HALTED")
            if not signal_only and order_cost > limits.max_order_cost:
                return reject(name + "_ORDER_COST_EXCEEDED")
            if (
                not signal_only
                and not closing
                and usage.committed + incremental > limits.max_committed
            ):
                return reject(name + "_BUDGET_EXCEEDED")
            if usage.open_orders + (0 if replacing else 1) > global_limits.max_open_orders:
                return reject(name + "_OPEN_ORDERS_EXCEEDED")
            maximum = (
                limits.max_stock_quantity
                if intent.legs[0].asset_type == "STOCK"
                else limits.max_future_quantity
                if future
                else limits.max_option_quantity
            )
            if intent.quantity > maximum:
                return reject(name + "_QUANTITY_EXCEEDED")
            if usage.requests_last_minute >= global_limits.max_requests_per_minute:
                return reject(name + "_RATE_EXCEEDED")
        context = Context(intent, strategy_id, cost, previous_cost, account, strategy, now)
        try:
            for rule in self.extra_rules:
                reason = rule.check(context)
                if reason is not None:
                    if not isinstance(reason, str) or not reason or len(reason) > 128:
                        return reject("CUSTOM_RULE_ERROR")
                    return reject(reason)
        except Exception:
            return reject("CUSTOM_RULE_ERROR")
        return Assessment(
            True,
            cost=cost,
            expiry_max_loss=expiry_loss,
            risk_basis=(
                "PAPER_SIGNAL_ONLY"
                if signal_only
                else "FUTURES_CONTRACT_BUDGET"
                if future
                else "STOCK_MARKET_NOTIONAL_BUDGET"
                if stock_market
                else "STOCK_SHORT_NOTIONAL_BUDGET"
                if stock_short
                else "EXPIRY_MAX_LOSS"
            ),
        )
