"""Order approvals and execution facts. Only proven outcomes release unfilled holds."""

from decimal import Decimal

from oms_protocol.messages import ProtocolError

from portfolio_service.ledger import amount

ZERO = Decimal("0")
TERMINAL = {"CANCELLED", "REJECTED", "EXPIRED", "FILLED"}


def empty_order():
    return dict(view={}, reservations={}, ever_filled=False, executions={}, facts_version=1)


def eligible_reservations(order):
    return [
        r for r in order["reservations"].values() if not r.get("definitely_not_executed", False)
    ]


def reject_reservation(reservation):
    reservation["active"] = False
    reservation["definitely_not_executed"] = True


def facts_match(order):
    view = order["view"]
    if order.get("facts_version") != 1:
        return not order.get("ever_filled") and not any(
            amount(q) for q in view.get("filled", {}).values()
        )
    totals = {x["instrument_id"]: ZERO for x in view["intent"]["legs"]}
    for record in order["executions"].values():
        if not record["superseded"]:
            totals[record["leg_id"]] += amount(record["quantity"])
    return all(totals.get(k, ZERO) == amount(v) for k, v in view.get("filled", {}).items()) and all(
        q == amount(view.get("filled", {}).get(k, "0")) for k, q in totals.items()
    )


def execution(order, payload):
    if not order["view"] or not eligible_reservations(order):
        return "UNRESERVED_EXECUTION"
    records = order["executions"]
    eid = payload["execution_id"]
    old = records.get(eid)
    if payload["leg_id"] not in {x["instrument_id"] for x in order["view"]["intent"]["legs"]}:
        raise ProtocolError("UNKNOWN_EXECUTION_LEG")
    if payload["update_kind"] == "FEE":
        if old is None or old["leg_id"] != payload["leg_id"]:
            raise ProtocolError("FEE_WITHOUT_EXECUTION")
        revision = payload["fee_revision"]
        if not isinstance(revision, int) or revision < 1:
            raise ProtocolError("INVALID_FEE_REVISION")
        fee = amount(payload["fees"])
        if fee < 0:
            raise ProtocolError("INVALID_FEE")
        if revision < old["fee_revision"]:
            return None
        if revision == old["fee_revision"] and fee != amount(old["fees"]):
            raise ProtocolError("FEE_REVISION_CONFLICT")
        old.update(fees=str(fee), fee_revision=revision)
    else:
        record = {k: payload[k] for k in ("leg_id", "quantity", "price", "fees", "correction_of")}
        for key in ("quantity", "price", "fees"):
            if amount(record[key]) < 0:
                raise ProtocolError("INVALID_EXECUTION_AMOUNT")
            record[key] = str(amount(record[key]))
        if old:
            if any(old[k] != record[k] for k in ("leg_id", "quantity", "price", "correction_of")):
                raise ProtocolError("EXECUTION_ID_CONFLICT")
            return None
        previous = records.get(record["correction_of"])
        if record["correction_of"]:
            if not previous or previous["superseded"] or previous["leg_id"] != record["leg_id"]:
                raise ProtocolError("INVALID_EXECUTION_CORRECTION")
            previous["superseded"] = True
        elif amount(record["quantity"]) == 0:
            raise ProtocolError("ZERO_FILL_WITHOUT_CORRECTION")
        records[eid] = record | dict(
            superseded=False, fee_revision=payload.get("fee_revision") or 0
        )
        order["ever_filled"] = True
    buffer = max(amount(r["fee_buffer"]) for r in eligible_reservations(order))
    if sum((amount(r["fees"]) for r in records.values() if not r["superseded"]), ZERO) > buffer:
        return "FEE_BUFFER_EXCEEDED"
    return None


def project(order, payload):
    if order.get("facts_version") != 1:
        if not facts_match(order):
            return "LEGACY_EXECUTIONS_REQUIRE_RECONCILIATION"
        order.update(executions={}, facts_version=1)
    kind = payload["kind"]
    if kind == "risk_resolution":
        if payload["disposition"] == "NOT_DISPATCHED":
            reservation = order["reservations"].get(payload["request_id"])
            if reservation:
                reject_reservation(reservation)
            if order["ever_filled"] and not eligible_reservations(order):
                return "UNRESERVED_EXECUTION"
        return None
    if kind == "execution":
        return execution(order, payload)
    if kind != "order_update":
        raise ProtocolError("UNKNOWN_ACCOUNT_EVENT")
    view = payload["state"]
    if not view:
        return None
    previous = order["view"]
    omitted = {"limit_amount", "decision_time", "deadline_at", "quote_references"}
    if previous and (
        view["strategy_instance_id"] != previous["strategy_instance_id"]
        or view["revision"] < previous["revision"]
        or {k: v for k, v in view["intent"].items() if k not in omitted}
        != {k: v for k, v in previous["intent"].items() if k not in omitted}
    ):
        raise ProtocolError("ORDER_PROJECTION_CONFLICT")
    order["view"] = view
    if view["consistency"] == "RECONCILIATION_REQUIRED" or view["dispatch_certainty"] == "UNKNOWN":
        return "ORDER_RECONCILIATION_REQUIRED"
    if not facts_match(order):
        return "EXECUTION_VIEW_MISMATCH"
    requires_hold = (
        view["dispatch_certainty"] in {"PREPARED", "ACKNOWLEDGED"}
        or view["status"] in {"DISPATCH_PENDING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED"}
        or order["ever_filled"]
    )
    if requires_hold and not order["reservations"]:
        return "UNRESERVED_ORDER"
    required = {view["intent_revision"]} if requires_hold else set()
    if view.get("pending_stage") == "EXECUTION_PENDING":
        required.add(view["pending_intent_revision"])
    if not required.issubset({r["revision"] for r in eligible_reservations(order)}):
        return "UNRESERVED_INTENT_REVISION"
    if payload["status"] == "REPLACE_REJECTED":
        for r in order["reservations"].values():
            if r["revision"] > view["intent_revision"]:
                reject_reservation(r)
    if view["status"] in TERMINAL and view["consistency"] == "CONSISTENT":
        for r in order["reservations"].values():
            r["active"] = False
    elif requires_hold:
        # A late correction can reopen a previously filled order. Re-acquire its remainder.
        for r in eligible_reservations(order):
            r["active"] = True
    return None
