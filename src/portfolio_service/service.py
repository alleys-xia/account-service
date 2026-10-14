"""Transactional account replay and projections. Broker differences never rewrite the ledger."""

from datetime import datetime
from decimal import Decimal

from oms_protocol.common import digest, utcnow
from oms_protocol.messages import ProtocolError
from psycopg.types.json import Jsonb

from .ledger import amount, fold, record
from .lots import build_lots


class Service:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.last_sync = None

    def scope(self, value):
        if (value.environment, value.account_id) != (
            self.settings.environment,
            self.settings.account_id,
        ):
            raise ProtocolError("SCOPE_MISMATCH")
        if hasattr(value, "mode") and value.mode != self.settings.mode:
            raise ProtocolError("EXECUTION_DOMAIN_MISMATCH")

    async def bind_head(self, epoch, head, blocked):
        async with self.store.transaction():
            old = await self.store.meta("feed_epoch")
            seq = await self.store.meta("account_seq", 0)
            bad = (old and old != epoch) or seq > head
            if bad:
                await self.store.set_meta("blocked", "FEED_EPOCH_OR_CURSOR_MISMATCH")
            else:
                await self.store.set_meta("feed_epoch", epoch)
                await self.store.set_meta("oms_blocked", blocked)
        if bad:
            raise ProtocolError("FEED_EPOCH_OR_CURSOR_MISMATCH")

    async def cursor(self):
        async with self.store.transaction():
            return await self.store.meta("feed_epoch"), await self.store.meta("account_seq", 0)

    async def revision(self):
        async with self.store.transaction():
            return await self.store.meta("ledger_revision", 0)

    async def apply(self, event):
        try:
            async with self.store.transaction():
                self.scope(event)
                if event.feed_epoch != await self.store.meta("feed_epoch"):
                    raise ProtocolError("FEED_EPOCH_MISMATCH")
                seq, hashed = (
                    await self.store.meta("account_seq", 0),
                    digest(event.model_dump(mode="json")),
                )
                if event.account_seq <= seq:
                    old = await self.store.one(
                        "SELECT hash FROM portfolio.events WHERE seq=%s", (event.account_seq,)
                    )
                    if not old or old["hash"] != hashed:
                        raise ProtocolError("ACCOUNT_EVENT_CONFLICT")
                    return
                if event.account_seq != seq + 1:
                    raise ProtocolError("ACCOUNT_EVENT_GAP")
                payload = event.payload
                if payload["kind"] == "order_update" and payload.get("state"):
                    order = payload["state"]
                    if (
                        order["order_id"] != payload["order_id"]
                        or order["strategy_instance_id"] not in self.settings.strategies
                    ):
                        raise ProtocolError("ORDER_OWNERSHIP_MISMATCH")
                    old = await self.store.order(order["order_id"])
                    if old and (
                        old["strategy_instance_id"] != order["strategy_instance_id"]
                        or old["intent"]["legs"] != order["intent"]["legs"]
                    ):
                        raise ProtocolError("ORDER_LEG_IDENTITY_CHANGED")
                    if order["intent"].get("currency", "USD") != "USD":
                        raise ProtocolError("UNSUPPORTED_CURRENCY")
                    await self.store.save_order(order["order_id"], order)
                elif payload["kind"] == "execution":
                    await self.execution(payload, event.account_seq)
                    await self.store.set_meta(
                        "ledger_revision", await self.store.meta("ledger_revision", 0) + 1
                    )
                elif payload["kind"] not in {"order_update", "risk_resolution"}:
                    raise ProtocolError("UNKNOWN_ACCOUNT_EVENT")
                await self.store.db.execute(
                    "INSERT INTO portfolio.events VALUES (%s,%s,%s)",
                    (event.account_seq, hashed, Jsonb(event.model_dump(mode="json"))),
                )
                await self.store.set_meta("account_seq", event.account_seq)
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            reason = error.code if isinstance(error, ProtocolError) else "INVALID_ACCOUNT_EVENT"
            # Roll back every partial mutation before persisting the failure marker.
            async with self.store.transaction():
                await self.store.set_meta("blocked", reason)
            raise ProtocolError(reason) from None

    async def execution(self, payload, seq):
        order = await self.store.order(payload["order_id"])
        if order is None:
            raise ProtocolError("UNKNOWN_EXECUTION_ORDER")
        eid = payload["execution_id"]
        row = await self.store.one("SELECT data FROM portfolio.executions WHERE id=%s", (eid,))
        old = row["data"] if row else None
        if old and (
            old["order_id"] != payload["order_id"] or old["instrument"] != payload["leg_id"]
        ):
            raise ProtocolError("EXECUTION_OWNERSHIP_CONFLICT")
        if payload["update_kind"] == "FEE":
            if old is None:
                raise ProtocolError("FEE_WITHOUT_EXECUTION")
            revision, fee = payload["fee_revision"], amount(payload["fees"])
            if revision < old["fee_revision"]:
                return
            if revision == old["fee_revision"]:
                if fee != amount(old["fees"]):
                    raise ProtocolError("FEE_REVISION_CONFLICT")
                return
            if fee < 0:
                raise ProtocolError("INVALID_FEE")
            data = old | dict(fees=str(fee), fee_revision=revision)
        elif payload["update_kind"] in {"FILL", "CORRECTION"}:
            data = record(payload, order, seq, self.settings.instruments.get(payload["leg_id"]))
            if old:
                keys = (
                    "order_id",
                    "strategy",
                    "instrument",
                    "side",
                    "quantity",
                    "price",
                    "multiplier",
                    "correction_of",
                )
                numeric = {"quantity", "price", "multiplier"}
                if any(
                    (amount(old[k]) != amount(data[k])) if k in numeric else old[k] != data[k]
                    for k in keys
                ):
                    raise ProtocolError("EXECUTION_ID_CONFLICT")
                return
            if data["correction_of"]:
                row = await self.store.one(
                    "SELECT data FROM portfolio.executions WHERE id=%s", (data["correction_of"],)
                )
                previous = row["data"] if row else None
                if (
                    not previous
                    or previous["superseded"]
                    or any(
                        previous[k] != data[k]
                        for k in ("order_id", "strategy", "instrument", "side", "multiplier")
                    )
                ):
                    raise ProtocolError("INVALID_EXECUTION_CORRECTION")
                data["effective_seq"] = previous["effective_seq"]
                await self.store.db.execute(
                    "UPDATE portfolio.executions SET data=%s WHERE id=%s",
                    (Jsonb(previous | dict(superseded=True)), data["correction_of"]),
                )
        else:
            raise ProtocolError("UNSUPPORTED_EXECUTION_EVENT")
        await self.store.db.execute(
            "INSERT INTO portfolio.executions VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (eid, data["strategy"], data["instrument"], Jsonb(data)),
        )
        rows = await self.store.all(
            "SELECT data FROM portfolio.executions WHERE strategy=%s AND instrument=%s ORDER BY (data->>'effective_seq')::bigint,id",
            (data["strategy"], data["instrument"]),
        )
        records = [x["data"] for x in rows]
        if len({x["instrument_version"] for x in records}) != 1:
            raise ProtocolError("INSTRUMENT_VERSION_CHANGED")
        position = fold(records) | dict(
            instrument_id=data["instrument"],
            instrument_version=data["instrument_version"],
            strategy_instance_id=data["strategy"],
        )
        await self.store.db.execute(
            "INSERT INTO portfolio.positions VALUES (%s,%s,%s) ON CONFLICT(strategy,instrument) DO UPDATE SET data=excluded.data",
            (data["strategy"], data["instrument"], Jsonb(position)),
        )

    async def accept_broker(self, snapshot, revision):
        self.scope(snapshot)
        async with self.store.transaction():
            if snapshot.feed_epoch != await self.store.meta("feed_epoch"):
                raise ProtocolError("BROKER_FEED_EPOCH_MISMATCH")
            value = snapshot.model_dump(mode="json")
            old = await self.store.one(
                "SELECT hash FROM portfolio.broker_snapshots WHERE id=%s", (snapshot.snapshot_id,)
            )
            if old and old["hash"] != digest(value):
                raise ProtocolError("BROKER_SNAPSHOT_CONFLICT")
            if old:
                return
            await self.store.db.execute(
                "INSERT INTO portfolio.broker_snapshots(id,hash,data) VALUES (%s,%s,%s)",
                (snapshot.snapshot_id, digest(value), Jsonb(value)),
            )
            await self.store.set_meta("broker", dict(snapshot=value, ledger_revision=revision))
            await self.store.set_meta("broker_error", None)

    async def snapshot(self, strategy=None):
        if strategy is not None and strategy not in self.settings.strategies:
            raise ProtocolError("UNKNOWN_STRATEGY")
        async with self.store.transaction():
            rows = await self.store.all(
                "SELECT data FROM portfolio.positions ORDER BY strategy,instrument LIMIT %s",
                (self.settings.max_positions + 1,),
            )
            if len(rows) > self.settings.max_positions:
                raise ProtocolError("POSITION_SNAPSHOT_CAPACITY")
            positions = [x["data"] for x in rows]
            orders = await self.store.all("SELECT data FROM portfolio.orders")
            lot_error = None
            try:
                lots = [
                    x
                    for x in build_lots(r["data"] for r in orders).values()
                    if amount(x["risk_quantity"]) > 0 or x["state"] != "SETTLED"
                ]
                if len(lots) > self.settings.max_positions:
                    raise ProtocolError("POSITION_LOT_CAPACITY")
            except ProtocolError as exc:
                lots, lot_error = [], exc.code
                await self.store.set_meta("blocked", lot_error)
            seq, revision = (
                await self.store.meta("account_seq", 0),
                await self.store.meta("ledger_revision", 0),
            )
            epoch = await self.store.meta("feed_epoch")
            blocked = await self.store.meta("blocked") or await self.store.meta("oms_blocked")
            broker = await self.store.meta("broker")
            broker_error = await self.store.meta("broker_error")
            status, external, unmapped = self.reconcile(positions, broker, revision)
            if broker_error:
                status = "SOURCE_UNAVAILABLE"
            if blocked:
                status = "BLOCKED"
            sync_fresh = (
                self.last_sync is not None
                and (utcnow() - self.last_sync).total_seconds()
                <= self.settings.poll_interval + self.settings.rpc_timeout
            )
            snapshot = dict(
                status="AVAILABLE" if epoch and not blocked else "UNAVAILABLE",
                items=[
                    p
                    for p in positions
                    if strategy is None or p["strategy_instance_id"] == strategy
                ],
                position_lots=[
                    x for x in lots if strategy is None or x["strategy_instance_id"] == strategy
                ],
                as_of=utcnow().isoformat(),
                applied_account_seq=seq,
                feed_epoch=epoch,
                strategy_seq=revision,
                reconciliation_status=status,
                reason=blocked or broker_error,
            )
            result = dict(
                mode=self.settings.mode,
                account_id=self.settings.account_id,
                strategy=strategy,
                snapshot=snapshot,
            )
            if strategy is None:
                result.update(
                    blocked=blocked,
                    reconciliation_status=status,
                    ledger_revision=revision,
                    applied_account_seq=seq,
                    feed_epoch=epoch,
                    oms_sync_fresh=sync_fresh,
                    safe_for_new_risk=bool(sync_fresh and status == "POSITIONS_MATCHED"),
                    trade_ready=False,
                    external_positions=external,
                    unmapped_instruments=unmapped,
                    broker_snapshot=broker["snapshot"] if broker else None,
                )
            return result

    def reconcile(self, positions, broker, revision):
        if not broker:
            return "UNAVAILABLE", [], []
        value = broker["snapshot"]
        age = (utcnow() - datetime.fromisoformat(value["started_at"])).total_seconds()
        if age < -1 or age > self.settings.max_snapshot_age:
            return "STALE", [], []
        if not value["complete"] or not value["stable"] or broker["ledger_revision"] != revision:
            return "PENDING", [], []
        mapping = {x["instrument_id"]: x for x in value["catalog"]}
        internal, unmapped = {}, []
        for p in positions:
            qty = amount(p["quantity"])
            if not qty:
                continue
            m = mapping.get(p["instrument_id"])
            if (
                not m
                or m["instrument_version"] != p["instrument_version"]
                or amount(m["multiplier"]) != amount(p["multiplier"])
            ):
                unmapped.append(p["instrument_id"])
                continue
            internal[m["con_id"]] = internal.get(m["con_id"], Decimal(0)) + qty
        actual = {x["con_id"]: x for x in value["positions"]}
        managed = value.get("managed_positions")
        managed_mismatch = False
        if managed is not None:
            owned = {}
            for p in managed:
                m = mapping.get(p["instrument_id"])
                if (
                    not m
                    or any(p[key] != m[key] for key in ("con_id", "instrument_version"))
                    or amount(p["multiplier"]) != amount(m["multiplier"])
                ):
                    unmapped.append(p["instrument_id"])
                    continue
                owned[p["con_id"]] = amount(p["quantity"])
            managed_mismatch = {k: v for k, v in internal.items() if v} != {
                k: v for k, v in owned.items() if v
            }
        external = []
        for con_id in sorted(set(internal) | set(actual)):
            row = actual.get(con_id, {})
            expected, observed = internal.get(con_id, Decimal(0)), amount(row.get("quantity", "0"))
            if expected != observed:
                external.append(
                    dict(
                        con_id=con_id,
                        symbol=row.get("symbol"),
                        broker_quantity=str(observed),
                        strategy_quantity=str(expected),
                        unassigned_quantity=str(observed - expected),
                    )
                )
        return (
            (
                "UNMAPPED"
                if unmapped
                else "MANAGED_POSITION_MISMATCH"
                if managed_mismatch
                else "EXTERNAL_OR_UNEXPLAINED"
                if external and managed is None
                else "POSITIONS_MATCHED"
            ),
            external,
            sorted(set(unmapped)),
        )
