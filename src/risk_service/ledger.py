"""Conservative order-level holds. Expiration of an approval never proves non-dispatch."""

from decimal import Decimal

from oms_protocol.messages import ProtocolError

ZERO = Decimal("0")
TERMINAL = {"CANCELLED", "REJECTED", "EXPIRED", "FILLED"}


def empty_order():
    return dict(view={}, reservations={}, ever_filled=False, filled_floor="0", fees={})


def commitment(order):
    return max(
        [Decimal(order["filled_floor"]), ZERO]
        + [Decimal(r["cost"]) for r in order["reservations"].values() if r["active"]]
    )


def eligible_reservations(order):
    # Active=False also includes legitimately dispatched orders after cancellation.
    # Only explicit non-dispatch/replacement rejection disqualifies a historical hold.
    return [
        r for r in order["reservations"].values() if not r.get("definitely_not_executed", False)
    ]


def retain_filled(order):
    if not order["ever_filled"]:
        return None
    eligible = eligible_reservations(order)
    if not eligible:
        return "UNRESERVED_EXECUTION"
    # Recompute after non-dispatch proof: a rejected increment is not position exposure.
    # All other historical valid holds remain eligible, even after correction to zero.
    order["filled_floor"] = str(max(Decimal(r["cost"]) for r in eligible))
    return None


def reject_reservation(reservation):
    reservation["active"] = False
    reservation["definitely_not_executed"] = True


def project(order, payload):
    kind = payload["kind"]
    if kind == "risk_resolution":
        if payload["disposition"] == "NOT_DISPATCHED":
            reservation = order["reservations"].get(payload["request_id"])
            if reservation:
                reject_reservation(reservation)
            return retain_filled(order)
        return None
    if kind == "execution":
        if not order["view"] or not order["reservations"]:
            return "UNRESERVED_EXECUTION"
        if payload["update_kind"] != "FEE":
            order["ever_filled"] = True
            issue = retain_filled(order)
            if issue:
                return issue
        order["fees"][payload["execution_id"]] = str(payload["fees"])
        maximum_buffer = max(Decimal(r["fee_buffer"]) for r in order["reservations"].values())
        if sum((Decimal(x) for x in order["fees"].values()), ZERO) > maximum_buffer:
            return "FEE_BUFFER_EXCEEDED"
        return None
    if kind != "order_update":
        raise ProtocolError("UNKNOWN_ACCOUNT_EVENT")
    view = payload["state"]
    if not view:
        return None  # command rejection before an order existed
    previous = order["view"]
    if previous and (
        view["strategy_instance_id"] != previous["strategy_instance_id"]
        or view["revision"] < previous["revision"]
    ):
        raise ProtocolError("ORDER_PROJECTION_CONFLICT")
    order["view"] = view
    if view["consistency"] == "RECONCILIATION_REQUIRED" or view["dispatch_certainty"] == "UNKNOWN":
        return "ORDER_RECONCILIATION_REQUIRED"
    if any(Decimal(x) > 0 for x in view.get("filled", {}).values()):
        order["ever_filled"] = True
    requires_hold = (
        view["dispatch_certainty"] in {"PREPARED", "ACKNOWLEDGED"}
        or view["status"] in {"DISPATCH_PENDING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED"}
        or order["ever_filled"]
    )
    if requires_hold and not order["reservations"]:
        return "UNRESERVED_ORDER"
    required_revisions = {view["intent_revision"]} if requires_hold else set()
    if view.get("pending_stage") == "EXECUTION_PENDING":
        required_revisions.add(view["pending_intent_revision"])
    if not required_revisions.issubset({r["revision"] for r in eligible_reservations(order)}):
        return "UNRESERVED_INTENT_REVISION"
    if payload["status"] == "REPLACE_REJECTED":
        for r in order["reservations"].values():
            if r["revision"] > view["intent_revision"]:
                reject_reservation(r)
    issue = retain_filled(order)
    if issue:
        return issue
    if view["status"] in TERMINAL and view["consistency"] == "CONSISTENT":
        for r in order["reservations"].values():
            r["active"] = False
    return None
