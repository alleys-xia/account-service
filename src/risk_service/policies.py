"""Pure, extensible policy chain. Storage/admission atomicity cannot be overridden by a rule."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from oms_protocol.orders import OrderIntent

from .config import Settings

ZERO = Decimal("0")


@dataclass(frozen=True)
class Usage:
    committed: Decimal = ZERO
    open_orders: int = 0
    requests_last_minute: int = 0


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
    expiry_max_loss: Decimal = ZERO


class Engine:
    def __init__(self, settings: Settings, *, extra_rules: tuple[Rule, ...] = ()):
        self.settings, self.extra_rules = settings, extra_rules

    def evaluate(self, intent, strategy_id, account, strategy, previous_cost, now):
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
        for leg in intent.legs:
            if leg.position_effect != "OPEN" or (
                intent.structure == "SINGLE" and leg.side != "BUY"
            ):
                return reject("POSITION_SUPPORT_REQUIRED")
            reference = s.instruments.get(leg.instrument_id)
            if reference is None:
                return reject("UNKNOWN_INSTRUMENT")
            if (
                reference.asset_type != leg.asset_type
                or reference.version != leg.instrument_version
            ):
                return reject("INSTRUMENT_MISMATCH")
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
                if (
                    reference.expiry - now.astimezone(ZoneInfo("America/New_York")).date()
                ).days < s.min_option_dte:
                    return reject("OPTION_EXPIRY_TOO_CLOSE")
        multiplier = s.instruments[intent.legs[0].instrument_id].multiplier
        if intent.structure == "SINGLE":
            if intent.net_price_effect != "DEBIT" or intent.limit_amount <= 0:
                return reject("INVALID_DEBIT")
            base = expiry_loss = intent.limit_amount * multiplier * intent.quantity
        else:
            width = abs(intent.legs[0].strike - intent.legs[1].strike)
            if not ZERO < intent.limit_amount < width:
                return reject("INVALID_CREDIT")
            base = width * multiplier * intent.quantity
            expiry_loss = (width - intent.limit_amount) * multiplier * intent.quantity
        cost = base + s.fee_buffer_per_unit_per_leg * len(intent.legs) * intent.quantity
        incremental = max(ZERO, cost - previous_cost)
        for name, limits, usage in (
            ("ACCOUNT", s.account, account),
            ("STRATEGY", s.strategies[strategy_id], strategy),
        ):
            if limits.halted:
                return reject(name + "_HALTED")
            if cost > limits.max_order_cost:
                return reject(name + "_ORDER_COST_EXCEEDED")
            if usage.committed + incremental > limits.max_committed:
                return reject(name + "_BUDGET_EXCEEDED")
            if usage.open_orders + (1 if previous_cost == 0 else 0) > limits.max_open_orders:
                return reject(name + "_OPEN_ORDERS_EXCEEDED")
            maximum = (
                limits.max_stock_quantity
                if intent.legs[0].asset_type == "STOCK"
                else limits.max_option_quantity
            )
            if intent.quantity > maximum:
                return reject(name + "_QUANTITY_EXCEEDED")
            if usage.requests_last_minute >= limits.max_requests_per_minute:
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
        return Assessment(True, cost=cost, expiry_max_loss=expiry_loss)
