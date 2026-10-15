"""Typed intent/report contracts. These do not qualify contracts or route orders."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_serializer,
    model_validator,
)

from .common import Identifier, Token, utcnow


def decimal_input(value):
    if isinstance(value, (float, bool)):
        raise ValueError("use Decimal or a decimal string, not float/bool")
    return value


Amount = Annotated[Decimal, BeforeValidator(decimal_input), Field(allow_inf_nan=False)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class OrderLeg(FrozenModel):
    instrument_id: Token
    instrument_version: int = Field(default=1, ge=1)
    asset_type: Literal["STOCK", "OPTION", "FUTURE"]
    side: Literal["BUY", "SELL"]
    ratio: int = Field(default=1, ge=1)
    position_effect: Literal["OPEN", "CLOSE"] = "OPEN"
    quantity_unit: Literal["SHARES", "CONTRACTS"]
    metadata_status: Literal["PROVISIONAL", "VERIFIED"] = "PROVISIONAL"
    underlying: str | None = Field(default=None, max_length=64)
    expiry: date | None = None
    right: Literal["CALL", "PUT"] | None = None
    strike: Amount | None = Field(default=None, gt=0)
    underlying_type: Literal["EQUITY", "INDEX"] = "EQUITY"
    settlement: Literal["PHYSICAL", "CASH"] = "PHYSICAL"
    exercise_style: Literal["AMERICAN", "EUROPEAN"] = "AMERICAN"

    @model_validator(mode="after")
    def valid_instrument(self):
        if self.asset_type == "OPTION":
            if (self.settlement, self.exercise_style) != (
                ("CASH", "EUROPEAN")
                if self.underlying_type == "INDEX"
                else ("PHYSICAL", "AMERICAN")
            ):
                raise ValueError("unsupported option settlement/exercise combination")
            if self.quantity_unit != "CONTRACTS" or any(
                x is None for x in (self.underlying, self.expiry, self.right, self.strike)
            ):
                raise ValueError("option requires option metadata and CONTRACTS unit")
        elif self.asset_type == "FUTURE":
            if (
                self.quantity_unit != "CONTRACTS"
                or self.underlying not in {"HSI", "MHI"}
                or self.expiry is None
                or self.underlying_type != "INDEX"
                or self.settlement != "CASH"
                or self.right is not None
                or self.strike is not None
            ):
                raise ValueError("future requires an actual HSI/MHI expiry and INDEX/CASH metadata")
        elif self.quantity_unit != "SHARES":
            raise ValueError("stock requires SHARES unit")
        return self


class OrderIntent(FrozenModel):
    structure: Literal["SINGLE", "VERTICAL_CREDIT", "VERTICAL_CLOSE"] = "SINGLE"
    close_position_id: Identifier | None = None
    legs: tuple[OrderLeg, ...] = Field(min_length=1, max_length=2)
    quantity: Amount = Field(gt=0)
    order_type: Literal["LIMIT"] = "LIMIT"
    limit_amount: Amount = Field(ge=0)
    currency: Literal["USD", "HKD"] = "USD"
    price_unit: Literal["PER_SHARE_OR_COMBO_UNIT", "INDEX_POINTS"] = "PER_SHARE_OR_COMBO_UNIT"
    net_price_effect: Literal["DEBIT", "CREDIT"] = "DEBIT"
    time_in_force: Literal["DAY", "GTC", "IOC"] = "DAY"
    decision_time: AwareDatetime = Field(default_factory=utcnow)
    deadline_at: AwareDatetime
    quote_references: tuple[dict[str, Any], ...] = Field(default=(), max_length=16)

    @model_serializer(mode="wrap")
    def wire(self, handler):
        value = handler(self)
        # Preserve canonical hashes of existing OPEN intents and nested commands.
        if self.close_position_id is None:
            value.pop("close_position_id", None)
        return value

    @model_validator(mode="after")
    def valid_structure(self):
        if len(self.model_dump_json().encode()) > 16384:
            raise ValueError("intent exceeds the 16 KiB protocol limit")
        if self.deadline_at <= self.decision_time:
            raise ValueError("deadline must follow decision time")
        if self.quantity != self.quantity.to_integral_value():
            raise ValueError("v1 supports whole shares/contracts only")
        if len({leg.instrument_id for leg in self.legs}) != len(self.legs):
            raise ValueError("each leg requires a distinct instrument identity")
        closing = self.close_position_id is not None
        if any((x.asset_type == "FUTURE") != (self.currency == "HKD") for x in self.legs):
            raise ValueError("HSI/MHI futures require HKD; US stocks/options require USD")
        if (self.price_unit == "INDEX_POINTS") != (self.legs[0].asset_type == "FUTURE"):
            raise ValueError("futures require INDEX_POINTS price unit")
        if any(x.position_effect != ("CLOSE" if closing else "OPEN") for x in self.legs):
            raise ValueError("all close legs require a position target; effects cannot be mixed")
        if self.structure == "SINGLE":
            if len(self.legs) != 1 or self.legs[0].ratio != 1:
                raise ValueError("SINGLE requires one ratio-1 leg")
            if self.legs[0].asset_type == "FUTURE":
                if self.net_price_effect != ("DEBIT" if self.legs[0].side == "BUY" else "CREDIT"):
                    raise ValueError("future BUY requires DEBIT, SELL requires CREDIT")
            elif closing and (self.legs[0].side != "SELL" or self.net_price_effect != "CREDIT"):
                raise ValueError("single close supports SELL/CREDIT against a long lot only")
        else:
            if closing != (self.structure == "VERTICAL_CLOSE"):
                raise ValueError("vertical structure must match its position effect")
            if len(self.legs) != 2 or self.net_price_effect != ("DEBIT" if closing else "CREDIT"):
                raise ValueError("vertical requires two legs, CREDIT open or DEBIT close")
            short = next((x for x in self.legs if x.side == "SELL"), None)
            long = next((x for x in self.legs if x.side == "BUY"), None)
            if short is None or long is None:
                raise ValueError("vertical credit requires buy and sell")
            if any(x.asset_type != "OPTION" or x.ratio != 1 for x in self.legs):
                raise ValueError("vertical requires ratio-1 option legs")
            if (short.underlying, short.expiry, short.right, short.position_effect) != (
                long.underlying,
                long.expiry,
                long.right,
                long.position_effect,
            ):
                raise ValueError("vertical legs must share underlying/expiry/right/effect")
            if closing:
                short, long = long, short  # Original short/long identities.
            if (short.right == "CALL" and short.strike >= long.strike) or (
                short.right == "PUT" and short.strike <= long.strike
            ):
                raise ValueError("expected bear call or bull put strike direction")
        return self

    @classmethod
    def vertical_credit(cls, short: OrderLeg, long: OrderLeg, **kwargs):
        return cls(
            structure="VERTICAL_CREDIT", legs=(short, long), net_price_effect="CREDIT", **kwargs
        )

    @classmethod
    def close_position(cls, position_id: str, opening: "OrderIntent", **kwargs):
        """Build the inverse intent; Risk still verifies ownership and available quantity."""
        if opening.close_position_id or (
            opening.structure == "SINGLE"
            and opening.legs[0].side != "BUY"
            and opening.legs[0].asset_type != "FUTURE"
        ):
            raise ValueError("expected a long single or credit vertical opening intent")
        if kwargs.pop("currency", opening.currency) != opening.currency:
            raise ValueError("close currency must match opening")
        if kwargs.pop("price_unit", opening.price_unit) != opening.price_unit:
            raise ValueError("close price unit must match opening")
        legs = tuple(
            leg.model_copy(
                update={"side": "SELL" if leg.side == "BUY" else "BUY", "position_effect": "CLOSE"}
            )
            for leg in opening.legs
        )
        return cls(
            structure="SINGLE" if opening.structure == "SINGLE" else "VERTICAL_CLOSE",
            close_position_id=position_id,
            legs=legs,
            net_price_effect=(
                "CREDIT" if opening.structure == "SINGLE" and legs[0].side == "SELL" else "DEBIT"
            ),
            currency=opening.currency,
            price_unit=opening.price_unit,
            **kwargs,
        )


class CommandHandle(FrozenModel):
    command_id: str
    client_order_id: str
    local_state: Literal["STAGED", "RECORDED"] = "STAGED"


class OrderUpdate(FrozenModel):
    kind: Literal["order_update"] = "order_update"
    event_id: str
    order_id: str
    revision: int = Field(ge=1)
    command_id: str
    status: Literal[
        "ACCEPTED",
        "REJECTED",
        "CANCELLED",
        "PARTIALLY_FILLED",
        "FILLED",
        "REPLACED",
        "RISK_PENDING",
        "SUBMITTED",
        "PENDING_CANCEL",
        "CANCEL_REJECTED",
        "REPLACE_PENDING",
        "REPLACE_REJECTED",
        "UNKNOWN",
        "EXPIRED",
        "RECONCILIATION_REQUIRED",
    ]
    reason: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    transport: Literal["MOCK", "OMS"] = "MOCK"
    feed_epoch: str = "mock-v1"
    strategy_seq: int = Field(default=1, ge=1)


class ExecutionEvent(FrozenModel):
    kind: Literal["execution"] = "execution"
    event_id: str
    execution_id: str
    order_id: str
    leg_id: str
    quantity: Amount = Field(ge=0)
    price: Amount = Field(ge=0)
    fees: Amount = Field(default=Decimal("0"), ge=0)
    correction_of: str | None = None
    fee_revision: int | None = Field(default=None, ge=1)
    update_kind: Literal["FILL", "CORRECTION", "FEE"] = "FILL"
    transport: Literal["MOCK", "OMS"] = "MOCK"
    feed_epoch: str = "mock-v1"
    strategy_seq: int = Field(default=1, ge=1)


class ViewSnapshot(FrozenModel):
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    items: tuple[dict[str, Any], ...] = ()
    position_lots: tuple[dict[str, Any], ...] = ()
    as_of: AwareDatetime = Field(default_factory=utcnow)
    applied_account_seq: int | None = None
    strategy_seq: int | None = None
    feed_epoch: str | None = None
    reconciliation_status: str = "UNAVAILABLE"
    reason: str | None = None


class PositionUpdate(FrozenModel):
    kind: Literal["position_update"] = "position_update"
    event_id: str
    snapshot: ViewSnapshot
    transport: Literal["MOCK", "OMS"] = "MOCK"


class RiskEvent(FrozenModel):
    kind: Literal["risk_event"] = "risk_event"
    event_id: str
    code: str
    message: str
    transport: Literal["MOCK", "OMS"] = "MOCK"
