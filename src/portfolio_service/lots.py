"""Pure opening-order lots. No database access or promise of available-to-close quantity."""

from decimal import Decimal

from oms_protocol.messages import ProtocolError
from oms_protocol.orders import OrderIntent

from .ledger import amount

ZERO = Decimal(0)


def units(view):
    """Per-leg effective filled units, including execution corrections."""
    result = []
    legs = {x["instrument_id"]: x for x in view["intent"]["legs"]}
    if set(view.get("filled", {})) - set(legs):
        raise ProtocolError("UNKNOWN_FILLED_LEG")
    for identity, leg in legs.items():
        qty = amount(view.get("filled", {}).get(identity, "0")) / leg["ratio"]
        if qty < 0 or qty > amount(view["intent"]["quantity"]):
            raise ProtocolError("INVALID_FILLED_QUANTITY")
        result.append(qty)
    return result


def validate_close(intent, strategy, lot):
    if lot is None:
        raise ProtocolError("UNKNOWN_POSITION")
    if lot["strategy_instance_id"] != strategy:
        raise ProtocolError("POSITION_OWNER_MISMATCH")
    opening = OrderIntent.model_validate(lot["opening_intent"])
    if (
        opening.structure == "SINGLE"
        and opening.legs[0].side != "BUY"
        and opening.legs[0].asset_type != "FUTURE"
    ):
        raise ProtocolError("UNSUPPORTED_POSITION")
    if intent.currency != opening.currency:
        raise ProtocolError("POSITION_CURRENCY_MISMATCH")
    expected = OrderIntent.close_position(
        lot["position_id"],
        opening,
        quantity=intent.quantity,
        limit_amount=intent.limit_amount,
        decision_time=intent.decision_time,
        deadline_at=intent.deadline_at,
    )
    if (intent.structure, intent.close_position_id) != (
        expected.structure,
        expected.close_position_id,
    ):
        raise ProtocolError("POSITION_STRUCTURE_MISMATCH")
    fields = {"side", "position_effect", "metadata_status"}
    original = {x.instrument_id: x for x in opening.legs}
    if set(original) != {x.instrument_id for x in intent.legs}:
        raise ProtocolError("POSITION_LEGS_MISMATCH")
    for leg in intent.legs:
        old = original[leg.instrument_id]
        if (
            leg.side == old.side
            or leg.position_effect != "CLOSE"
            or leg.model_dump(exclude=fields) != old.model_dump(exclude=fields)
        ):
            raise ProtocolError("POSITION_LEGS_MISMATCH")


def build_lots(views):
    """Conservative quantities per opening order, never net across lots or strategies."""
    views = list(views)
    lots = {}
    for v in views:
        intent = v["intent"]
        if intent.get("close_position_id"):
            continue
        opened = units(v)
        lots[v["order_id"]] = dict(
            position_id=v["order_id"],
            strategy_instance_id=v["strategy_instance_id"],
            opening_intent=intent,
            opened_quantity=str(min(opened)),
            legs=[
                dict(
                    instrument_id=leg["instrument_id"],
                    instrument_version=leg["instrument_version"],
                    side=leg["side"],
                    ratio=leg["ratio"],
                    opened=str(qty),
                    closed="0",
                    remaining=str(qty),
                )
                for leg, qty in zip(intent["legs"], opened)
            ],
            state="SETTLED" if v["consistency"] == "CONSISTENT" else "UNSETTLED",
        )
    for v in views:
        target = v["intent"].get("close_position_id")
        if not target or not any(units(v)):
            continue  # Unfilled rejected attempts must not corrupt the position projection.
        lot = lots.get(target)
        validate_close(OrderIntent.model_validate(v["intent"]), v["strategy_instance_id"], lot)
        remaining = {leg["instrument_id"]: leg for leg in lot["legs"]}
        for leg, filled in zip(v["intent"]["legs"], units(v)):
            row = remaining[leg["instrument_id"]]
            row["closed"] = str(amount(row["closed"]) + filled)
            row["remaining"] = str(amount(row["opened"]) - amount(row["closed"]))
            if amount(row["remaining"]) < 0:
                raise ProtocolError("OVER_CLOSED_POSITION")
        if v["consistency"] != "CONSISTENT":
            lot["state"] = "UNSETTLED"
    for lot in lots.values():
        remaining = [amount(x["remaining"]) for x in lot["legs"]]
        if len(set(remaining)) != 1:
            lot["state"] = "UNSETTLED"
        lot["remaining_quantity"] = str(min(remaining))
        lot["risk_quantity"] = str(max(remaining))
        lot["closed_quantity"] = str(min(amount(x["closed"]) for x in lot["legs"]))
    return lots
