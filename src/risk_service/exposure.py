"""Budget transfer and live close holds computed under the admission transaction."""

from decimal import Decimal

from oms_protocol.orders import STOCK_SHORT_BUDGET_FACTOR

from portfolio_service.lots import units

from .ledger import eligible_reservations

ZERO = Decimal(0)


def active(order):
    return any(r["active"] for r in eligible_reservations(order))


def unit_cost(order):
    reserved = max(
        [ZERO]
        + [
            Decimal(r["cost"]) / Decimal(r.get("quantity", order["view"]["intent"]["quantity"]))
            for r in eligible_reservations(order)
        ]
    )
    short_reservations = [
        r for r in eligible_reservations(order) if r.get("stock_short_budget_per_share") is not None
    ]
    if not short_reservations:
        return reserved
    # A sell limit is a minimum. Higher fills must increase, never reduce, the hold.
    filled_price = max(
        [ZERO]
        + [
            Decimal(r["price"])
            for r in order["executions"].values()
            if not r["superseded"] and Decimal(r["quantity"]) > 0
        ]
    )
    fee_per_share = max(
        Decimal(r["fee_buffer"]) / Decimal(r["quantity"]) for r in short_reservations
    )
    return max(reserved, STOCK_SHORT_BUDGET_FACTOR * filled_price + fee_per_share)


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
            currency=v["intent"].get("currency", "USD"),
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
