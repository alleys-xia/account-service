"""Pure order transitions. Broker adapters and persistence stay outside this module."""

from decimal import Decimal
from typing import Any, Literal

from oms_protocol.common import digest
from oms_protocol.messages import ProtocolError
from oms_protocol.orders import ExecutionEvent, OrderIntent
from pydantic import BaseModel, ConfigDict, Field

TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    client_order_id: str
    strategy_instance_id: str
    account_id: str
    environment: str = "paper"
    intent: OrderIntent
    command_id: str
    session_token: str
    session_epoch: int
    revision: int = 0
    intent_revision: int = 1
    last_intent_revision: int = 1
    status: Literal[
        "RISK_PENDING",
        "DISPATCH_PENDING",
        "SUBMITTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
    ] = "RISK_PENDING"
    dispatch_certainty: Literal["NOT_SENT", "PREPARED", "ACKNOWLEDGED", "UNKNOWN"] = "NOT_SENT"
    cancel_state: Literal["NONE", "REQUESTED", "CONFIRMED", "REJECTED"] = "NONE"
    consistency: Literal["CONSISTENT", "LEGS_PENDING", "RECONCILIATION_REQUIRED"] = "CONSISTENT"
    pending: dict[str, Any] | None = None
    executions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    filled: dict[str, Decimal] = Field(default_factory=dict)
    reason: str | None = None

    def view(self):
        return self.model_dump(mode="json", exclude={"session_token", "executions", "pending"}) | {
            "pending_stage": self.pending.get("stage") if self.pending else None,
            "pending_request_id": self.pending.get("request_id") if self.pending else None,
            "pending_operation": self.pending.get("operation") if self.pending else None,
            "pending_intent_revision": self.pending.get("revision") if self.pending else None,
        }


def intent_hash(intent):
    return digest(intent.model_dump(mode="json"))


def validate_replacement(old, new):
    # New timing and quote references audit the new decision; economically only price may change.
    omitted = {"limit_amount", "decision_time", "deadline_at", "quote_references"}
    if old.model_dump(exclude=omitted) != new.model_dump(exclude=omitted):
        raise ProtocolError("REPLACE_PRICE_ONLY")


def _totals(order, *, corrected=False):
    totals = {leg.instrument_id: Decimal(0) for leg in order.intent.legs}
    for fill in order.executions.values():
        if not fill.get("superseded"):
            totals[fill["leg_id"]] += Decimal(fill["quantity"])
    for leg in order.intent.legs:
        if totals[leg.instrument_id] > order.intent.quantity * leg.ratio:
            raise ProtocolError("OVERFILL_REQUIRES_RECONCILIATION")
    order.filled = totals
    units = [totals[leg.instrument_id] / leg.ratio for leg in order.intent.legs]
    if order.consistency != "RECONCILIATION_REQUIRED":
        order.consistency = "CONSISTENT" if len(set(units)) == 1 else "LEGS_PENDING"
    if all(x == order.intent.quantity for x in units):
        order.status = "FILLED"
    elif order.cancel_state == "CONFIRMED":
        order.status = "CANCELLED"
    elif any(units):
        order.status = "PARTIALLY_FILLED"
    elif corrected:
        order.status = "SUBMITTED"


def apply_execution(original, report, command):
    """Return copied aggregate, execution events, and optional order notification status."""
    order = original.model_copy(deep=True)
    events = []
    kind = report.kind
    status = None
    if kind == "FILL":
        if report.leg_id not in {leg.instrument_id for leg in order.intent.legs}:
            raise ProtocolError("UNKNOWN_EXECUTION_LEG")
        fill = dict(
            leg_id=report.leg_id,
            quantity=str(report.quantity),
            price=str(report.price),
            fees=str(report.fees),
            correction_of=report.correction_of,
            superseded=False,
        )
        existing = order.executions.get(report.execution_id)
        if existing:
            # Fee updates and later corrections do not change the original fill identity.
            if any(
                existing[key] != fill[key]
                for key in ("leg_id", "quantity", "price", "correction_of")
            ):
                raise ProtocolError("EXECUTION_ID_CONFLICT")
            return order, [], None
        if report.correction_of:
            previous = order.executions.get(report.correction_of)
            if not previous or previous["superseded"] or previous["leg_id"] != report.leg_id:
                raise ProtocolError("UNKNOWN_EXECUTION_CORRECTION")
            previous["superseded"] = True
        order.executions[report.execution_id] = fill
        if order.status in {"REJECTED", "EXPIRED"}:
            order.consistency = "RECONCILIATION_REQUIRED"
        _totals(order, corrected=bool(report.correction_of))
        order.dispatch_certainty = "ACKNOWLEDGED"
        events.append(
            ExecutionEvent(
                event_id=report.event_id + ":execution",
                execution_id=report.execution_id,
                order_id=order.order_id,
                leg_id=report.leg_id,
                quantity=report.quantity,
                price=report.price,
                fees=report.fees,
                correction_of=report.correction_of,
                update_kind="CORRECTION" if report.correction_of else "FILL",
                transport="OMS",
            )
        )
        status = order.status
    elif kind == "FEE":
        fill = order.executions.get(report.execution_id)
        if not fill:
            raise ProtocolError("UNKNOWN_FEE_EXECUTION")
        previous_revision = fill.get("fee_revision", 0)
        if report.fee_revision < previous_revision:
            return order, [], None
        if report.fee_revision == previous_revision:
            if Decimal(fill["fees"]) != report.fees:
                raise ProtocolError("FEE_REVISION_CONFLICT")
            return order, [], None
        fill["fee_revision"] = report.fee_revision
        fill["fees"] = str(report.fees)
        events.append(
            ExecutionEvent(
                event_id=report.event_id + ":fee",
                execution_id=report.execution_id,
                order_id=order.order_id,
                leg_id=fill["leg_id"],
                quantity="0",
                price=fill["price"],
                fees=report.fees,
                update_kind="FEE",
                fee_revision=report.fee_revision,
                transport="OMS",
            )
        )
    elif kind == "ACK":
        if command.operation != "submit":
            raise ProtocolError("ACK_REQUIRES_SUBMIT")
        if order.status not in TERMINAL and order.status != "PARTIALLY_FILLED":
            order.status = "SUBMITTED"
            order.dispatch_certainty = "ACKNOWLEDGED"
            status = "SUBMITTED"
    elif kind == "CANCELLED":
        if command.operation != "cancel":
            raise ProtocolError("CANCEL_REPORT_COMMAND_MISMATCH")
        order.cancel_state = "CONFIRMED"
        if order.status != "FILLED":
            order.status = "CANCELLED"
        order.pending = None
        status = order.status
    elif kind == "CANCEL_REJECTED":
        if command.operation != "cancel":
            raise ProtocolError("CANCEL_REPORT_COMMAND_MISMATCH")
        if order.cancel_state != "CONFIRMED":
            order.cancel_state = "REJECTED"
            status = "CANCEL_REJECTED"
    elif kind in {"REPLACED", "REPLACE_REJECTED"}:
        if command.operation != "replace":
            raise ProtocolError("REPLACE_REPORT_COMMAND_MISMATCH")
        if order.pending and order.pending["revision"] == report.order_revision:
            if kind == "REPLACED" and order.status not in TERMINAL:
                order.intent = command.intent
                order.intent_revision = report.order_revision
            order.pending = None
            if order.status not in TERMINAL:
                status = kind
    elif kind in {"REJECTED", "NOT_SUBMITTED"}:
        if command.operation == "cancel":
            if order.cancel_state != "CONFIRMED":
                order.cancel_state = "REJECTED"
                status = "CANCEL_REJECTED"
        elif command.operation == "replace":
            if order.pending and order.pending["revision"] == report.order_revision:
                order.pending = None
                status = "REPLACE_REJECTED"
        else:
            if any(order.filled.values()) or order.dispatch_certainty == "ACKNOWLEDGED":
                raise ProtocolError("REJECTION_CONTRADICTS_EXECUTION")
            if order.status not in TERMINAL:
                order.status = "REJECTED"
                order.dispatch_certainty = "NOT_SENT" if kind == "NOT_SUBMITTED" else "ACKNOWLEDGED"
                status = "REJECTED"
    elif kind == "UNKNOWN":
        order.dispatch_certainty = "UNKNOWN"
        order.consistency = "RECONCILIATION_REQUIRED"
        if order.status not in TERMINAL:
            order.status = "UNKNOWN"
        status = "UNKNOWN"
    if status or events:
        order.reason = report.reason or None
    return order, events, status
