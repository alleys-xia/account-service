"""Pure moving-average ledger. Fees are expensed, not capitalized into unit cost."""

from decimal import Decimal

from oms_protocol.messages import ProtocolError


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ProtocolError("NON_FINITE_AMOUNT")
    return result


def fold(records):
    quantity = average = realized = fees = cash = Decimal(0)
    multiplier = None
    identity = None
    for row in records:
        if row.get("superseded"):
            continue
        current = (
            row.get("asset_type", "OPTION" if amount(row["multiplier"]) == 100 else "STOCK"),
            row.get("currency", "USD"),
        )
        if (
            current[0] == "FUTURE"
            and (current[1] != "HKD" or amount(row["multiplier"]) not in {10, 50})
        ) or (current[0] != "FUTURE" and current[1] != "USD"):
            raise ProtocolError("INVALID_LEDGER_ASSET_CURRENCY")
        if identity is not None and current != identity:
            raise ProtocolError("LEDGER_ASSET_CURRENCY_MISMATCH")
        identity = current
        size, price, fee, factor = (
            amount(row[k]) for k in ("quantity", "price", "fees", "multiplier")
        )
        if (
            size < 0
            or (size == 0 and not row.get("correction_of"))
            or price < 0
            or fee < 0
            or factor <= 0
            or row["side"] not in {"BUY", "SELL"}
        ):
            raise ProtocolError("INVALID_LEDGER_AMOUNT")
        if multiplier is not None and multiplier != factor:
            raise ProtocolError("MULTIPLIER_CHANGED")
        multiplier = factor
        delta = size if row["side"] == "BUY" else -size
        if current[0] != "FUTURE":
            cash -= delta * price * factor + fee
        fees += fee
        if size == 0:
            continue
        if quantity == 0 or quantity * delta > 0:
            average = (abs(quantity) * average + size * price) / (abs(quantity) + size)
        else:
            closed = min(abs(quantity), size)
            realized += closed * (price - average) * (1 if quantity > 0 else -1) * factor
            if size > abs(quantity):
                average = price
            elif size == abs(quantity):
                average = Decimal(0)
        quantity += delta
    result = dict(
        quantity=str(quantity),
        average_cost=str(average),
        multiplier=str(multiplier or 1),
        realized_pnl_gross=str(realized),
        fees=str(fees),
        realized_pnl_net=str(realized - fees),
        trade_cash_flow=None if identity and identity[0] == "FUTURE" else str(cash),
        currency=identity[1] if identity else "USD",
        unrealized_pnl=None,
    )
    if identity and identity[0] == "FUTURE":
        result.update(asset_type="FUTURE", settlement_status="FUTURES_SETTLEMENT_UNTRACKED")
    return result


def record(payload, order, seq, reference):
    legs = {x["instrument_id"]: x for x in order["intent"]["legs"]}
    leg = legs.get(payload["leg_id"])
    if leg is None:
        raise ProtocolError("UNKNOWN_EXECUTION_LEG")
    if (
        reference is None
        or reference.currency != order["intent"].get("currency", "USD")
        or reference.asset_type != leg["asset_type"]
        or reference.version != leg["instrument_version"]
    ):
        raise ProtocolError("INSTRUMENT_CATALOG_MISMATCH")
    factor = reference.multiplier
    if leg["asset_type"] not in {"STOCK", "OPTION", "FUTURE"} or factor not in (
        {10, 50} if leg["asset_type"] == "FUTURE" else {1 if leg["asset_type"] == "STOCK" else 100}
    ):
        raise ProtocolError("UNSUPPORTED_INSTRUMENT")
    return dict(
        order_id=order["order_id"],
        strategy=order["strategy_instance_id"],
        instrument=leg["instrument_id"],
        instrument_version=leg["instrument_version"],
        side=leg["side"],
        **({"asset_type": "FUTURE", "currency": "HKD"} if leg["asset_type"] == "FUTURE" else {}),
        multiplier=str(factor),
        quantity=str(amount(payload["quantity"])),
        price=str(amount(payload["price"])),
        fees=str(amount(payload.get("fees", "0"))),
        fee_revision=payload.get("fee_revision") or 0,
        superseded=False,
        effective_seq=seq,
        correction_of=payload.get("correction_of"),
    )
