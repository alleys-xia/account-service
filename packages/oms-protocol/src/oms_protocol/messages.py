"""Strict v1 messages for durable OMS, risk and execution boundaries."""

from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from .common import Identifier, Token, digest, uid, utcnow
from .orders import Amount, FrozenModel, OrderIntent


class Scoped(FrozenModel):
    schema_version: Literal[1] = 1
    environment: Literal["paper", "live", "replay"] = "paper"
    account_id: Token


class OrderCommand(Scoped):
    command_id: Identifier
    client_order_id: Identifier
    strategy_instance_id: Token
    session_token: Token
    session_epoch: int = Field(ge=1)
    operation: Literal["submit", "cancel", "replace"]
    deadline_at: AwareDatetime
    intent: OrderIntent | None = None
    order_id: Identifier | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    input_id: str = Field(default="", max_length=256)
    strategy_config_version: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def operation_fields(self):
        if self.operation == "submit":
            if (
                self.intent is None
                or self.order_id is not None
                or self.expected_revision is not None
            ):
                raise ValueError("submit requires only an intent")
        elif not self.order_id:
            raise ValueError("cancel/replace requires order_id")
        elif self.operation == "cancel" and (self.intent is not None or self.expected_revision):
            raise ValueError("cancel cannot modify intent")
        elif self.operation == "replace" and (
            self.intent is None or self.expected_revision is None
        ):
            raise ValueError("replace requires full intent and expected view revision")
        if self.intent and self.deadline_at != self.intent.deadline_at:
            raise ValueError("command and intent deadline must agree")
        return self

    def business_hash(self):
        return digest(self.model_dump(mode="json", exclude={"session_token", "session_epoch"}))


class RiskRequest(Scoped):
    message_id: Identifier
    order_id: Identifier
    strategy_instance_id: Token
    order_revision: int = Field(ge=1)
    intent: OrderIntent
    intent_hash: str = Field(min_length=1, max_length=64)
    required_account_seq: int = Field(ge=0)
    operation: Literal["submit", "replace"]
    mode: Literal["SIMULATION"] = "SIMULATION"


class RiskDecision(Scoped):
    event_id: Identifier
    request_id: Identifier
    order_id: Identifier
    order_revision: int = Field(ge=1)
    intent_hash: str = Field(min_length=1, max_length=64)
    approved: bool
    reservation_id: Identifier | None = None
    approved_quantity: Amount | None = Field(default=None, gt=0)
    approved_limit: Amount | None = Field(default=None, ge=0)
    valid_until: AwareDatetime
    applied_account_seq: int = Field(ge=0)
    reason: str = Field(default="", max_length=1024)
    mode: Literal["SIMULATION"] = "SIMULATION"

    @model_validator(mode="after")
    def approval_fields(self):
        if self.approved and any(
            x is None for x in (self.reservation_id, self.approved_quantity, self.approved_limit)
        ):
            raise ValueError("approval requires reservation and explicit bounds")
        return self


class ExecutionCommand(Scoped):
    command_id: Identifier
    client_command_id: Identifier
    order_id: Identifier
    strategy_instance_id: Token
    order_revision: int = Field(ge=1)
    operation: Literal["submit", "cancel", "replace"]
    intent: OrderIntent
    intent_hash: str = Field(min_length=1, max_length=64)
    reservation_id: Identifier | None = None
    valid_until: AwareDatetime
    session_epoch: int = Field(ge=1)
    mode: Literal["SIMULATION"] = "SIMULATION"


class ExecutionReport(Scoped):
    event_id: Identifier
    execution_command_id: Identifier
    order_id: Identifier
    order_revision: int = Field(ge=1)
    kind: Literal[
        "ACK",
        "FILL",
        "FEE",
        "CANCELLED",
        "REJECTED",
        "CANCEL_REJECTED",
        "REPLACED",
        "REPLACE_REJECTED",
        "UNKNOWN",
        "NOT_SUBMITTED",
    ]
    source_session: Token
    ingress_seq: int = Field(ge=1)
    execution_id: Identifier | None = None
    leg_id: Identifier | None = None
    quantity: Amount | None = Field(default=None, ge=0)
    price: Amount | None = Field(default=None, ge=0)
    fees: Amount = Field(default=Decimal("0"), ge=0)
    correction_of: Identifier | None = None
    fee_revision: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=1024)
    occurred_at: AwareDatetime = Field(default_factory=utcnow)
    mode: Literal["SIMULATION"] = "SIMULATION"

    @model_validator(mode="after")
    def fill_fields(self):
        if self.kind == "FILL":
            if any(x is None for x in (self.execution_id, self.leg_id, self.quantity, self.price)):
                raise ValueError("fill requires execution identity, leg, quantity and price")
            if self.quantity == 0 and self.correction_of is None:
                raise ValueError("zero quantity only permitted for a correction")
            if self.correction_of == self.execution_id:
                raise ValueError("execution cannot correct itself")
        if self.kind == "FEE" and (not self.execution_id or self.fee_revision is None):
            raise ValueError("fee update requires execution identity and durable fee_revision")
        if self.kind not in {"FILL", "FEE"} and any(
            x is not None
            for x in (self.execution_id, self.leg_id, self.quantity, self.price, self.correction_of)
        ):
            raise ValueError("status reports cannot contain executions")
        return self


class RpcRequest(Scoped):
    request_id: Identifier = Field(default_factory=uid)
    strategy_instance_id: Token
    operation: Literal["acquire", "renew", "release", "snapshot", "events", "command", "health"]
    run_id: Token | None = None
    session_token: Token | None = None
    session_epoch: int | None = Field(default=None, ge=1)
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    command_id: Identifier | None = None


class RpcResponse(FrozenModel):
    request_id: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ProtocolError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
