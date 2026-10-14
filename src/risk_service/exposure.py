"""Budget transfer and live close holds computed under the admission transaction."""

from decimal import Decimal

from portfolio_service.lots import units

from .ledger import eligible_reservations

ZERO = Decimal(0)


def active(order):
    return any(r["active"] for r in eligible_reservations(order))


def unit_cost(order):
    return max(
        [ZERO]
        + [
            Decimal(r["cost"]) / Decimal(r.get("quantity", order["view"]["intent"]["quantity"]))
            for r in eligible_reservations(order)
        ]
    )


def exposure(orders, lots):
    result = {}
    for oid, order in orders.items():
        v = order["view"]
        if not v:
            continue
        pending = position = ZERO
        if not v["intent"].get("close_position_id"):
            cost = unit_cost(order)
            pending = (
                max(ZERO, Decimal(v["intent"]["quantity"]) - min(units(v))) * cost
                if active(order)
                else ZERO
            )
            position = Decimal(lots[oid]["risk_quantity"]) * cost
        result[oid] = dict(
            pending=pending,
            position=position,
            total=pending + position,
            open=active(order),
            strategy=v["strategy_instance_id"],
        )
    return result


def close_reserved(orders, position_id, *, exclude=None):
    held = ZERO
    for oid, order in orders.items():
        v = order["view"]
        if (
            oid != exclude
            and v
            and v["intent"].get("close_position_id") == position_id
            and active(order)
        ):
            held += max(ZERO, Decimal(v["intent"]["quantity"]) - min(units(v)))
    return held


def available_to_close(orders, lots, position_id, *, exclude=None):
    lot = lots[position_id]
    if lot["state"] != "SETTLED":
        return ZERO
    return max(
        ZERO,
        Decimal(lot["remaining_quantity"]) - close_reserved(orders, position_id, exclude=exclude),
    )
