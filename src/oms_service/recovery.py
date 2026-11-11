"""Durable owner-loss cleanup using ordinary OMS commands and Portfolio proof."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from nats.errors import Error as NatsError
from oms_protocol.common import uid, utcnow
from oms_protocol.futures import FUTURE_POSITION_CAPS, MARKET_FUTURES
from oms_protocol.messages import OrderCommand, ProtocolError
from oms_protocol.orders import OrderIntent
from psycopg.types.json import Jsonb

from portfolio_service.lots import build_lots

from .domain import TERMINAL, Order


class Recovery:
    def __init__(self, service, allowlist):
        self.service, self.store, self.settings = service, service.store, service.settings
        self.allowlist = {key: tuple(value) for key, value in (allowlist or {}).items()}
        if self.allowlist and (
            self.settings.mode != "IB_PAPER"
            or not set(self.allowlist).issubset(self.settings.strategies)
            or any(not value or len(set(value)) != len(value) for value in self.allowlist.values())
        ):
            raise ProtocolError("INVALID_RECOVERY_SCOPE")
        self.snapshot = None
        self.max_snapshot_age = 30
        self.log = None

    async def current(self, strategy):
        row = await self.store.one(
            "SELECT data FROM oms.strategy_recoveries WHERE strategy=%s", (strategy,)
        )
        return row["data"] if row else None

    async def save(self, strategy, value):
        await self.store.db.execute(
            "INSERT INTO oms.strategy_recoveries VALUES (%s,%s) "
            "ON CONFLICT(strategy) DO UPDATE SET data=excluded.data",
            (strategy, Jsonb(value)),
        )

    async def orders(self, strategy):
        rows = await self.store.all(
            "SELECT data FROM oms.orders WHERE strategy=%s ORDER BY id", (strategy,)
        )
        return [Order.model_validate(row["data"]) for row in rows]

    def scoped(self, strategy, intent, instruments=None):
        return bool(
            intent.structure == "SINGLE"
            and len(intent.legs) == 1
            and intent.legs[0].instrument_id
            in (self.allowlist.get(strategy, ()) if instruments is None else instruments)
            and intent.legs[0].asset_type == "FUTURE"
            and intent.legs[0].underlying in MARKET_FUTURES
        )

    @staticmethod
    def public(value):
        return (
            {key: value.get(key) for key in ("recovery_id", "state", "reason")} if value else None
        )

    @staticmethod
    def active(value):
        return bool(value and value["state"] == "CLEANING")

    async def reason(self, strategy, value, reason):
        if value.get("reason") != reason:
            value["reason"] = reason
            await self.save(strategy, value)
            if self.log:
                self.log("strategy_recovery_waiting", strategy=strategy, error=reason)

    async def claim(self, strategy, *, force=False):
        """Caller holds OMS transaction. Claim and lease replacement commit together."""
        value = await self.current(strategy)
        if self.active(value) or strategy not in self.allowlist:
            return value
        row = await self.store.one("SELECT data FROM oms.sessions WHERE strategy=%s", (strategy,))
        owner = row["data"] if row else None
        if not owner:
            return value
        if not force and (
            datetime.fromisoformat(owner["expires_at"]) > utcnow()
            or (value and owner["run_id"] == value["lease"]["run_id"])
        ):
            return value
        recovery_id = uid()
        lease = dict(
            run_id="recovery-" + recovery_id,
            token=uid(),
            epoch=owner["epoch"] + 1,
            expires_at=(utcnow() + timedelta(seconds=self.settings.session_ttl)).isoformat(),
        )
        value = dict(
            recovery_id=recovery_id,
            state="CLEANING",
            reason="LATE_OWNED_EXECUTION" if force else "OWNER_SESSION_EXPIRED",
            fenced_epoch=owner["epoch"],
            lease=lease,
            pending_command=None,
            attempt=0,
            next_action_at=utcnow().isoformat(),
            feed_epoch=self.store.feed_epoch,
            instruments=sorted(
                set((value or {}).get("instruments", ())) | set(self.allowlist[strategy])
            ),
        )
        await self.store.db.execute(
            "INSERT INTO oms.sessions VALUES (%s,%s) "
            "ON CONFLICT(strategy) DO UPDATE SET data=excluded.data",
            (strategy, Jsonb(lease)),
        )
        await self.save(strategy, value)
        if self.log:
            self.log("strategy_recovery_started", strategy=strategy)
        return value

    async def renew(self, strategy, value):
        row = await self.store.one("SELECT data FROM oms.sessions WHERE strategy=%s", (strategy,))
        owner = row["data"] if row else None
        lease = value["lease"]
        if not owner or any(owner[key] != lease[key] for key in ("run_id", "token", "epoch")):
            raise ProtocolError("RECOVERY_LEASE_CONFLICT")
        lease["expires_at"] = (utcnow() + timedelta(seconds=self.settings.session_ttl)).isoformat()
        await self.store.db.execute(
            "UPDATE oms.sessions SET data=%s WHERE strategy=%s", (Jsonb(lease), strategy)
        )
        await self.save(strategy, value)

    def authorizes(self, value, command):
        return bool(
            self.active(value)
            and value.get("pending_command") == command.model_dump(mode="json")
            and command.session_token == value["lease"]["token"]
            and command.session_epoch == value["lease"]["epoch"]
        )

    async def observe(self, strategy):
        """Caller holds OMS transaction; late retired facts fence a successor immediately."""
        value = await self.current(strategy)
        if not value or value["state"] != "COMPLETE" or strategy not in self.allowlist:
            return
        orders = await self.orders(strategy)
        lots = build_lots(order.view() for order in orders)
        if any(
            not order.intent.close_position_id
            and order.session_epoch <= value["fenced_epoch"]
            and self.scoped(strategy, order.intent, value["instruments"])
            and Decimal(lots[order.order_id]["risk_quantity"]) > 0
            for order in orders
        ):
            await self.claim(strategy, force=True)

    async def action(self, strategy, value, *, order=None, lot=None):
        """Persist exact command before returning it to the normal admission boundary."""
        now = utcnow()
        value["attempt"] += 1
        identity = f"recovery-{value['recovery_id']}-{value['attempt']}"
        deadline = now + timedelta(seconds=30)
        fields = (
            dict(order_id=order.order_id)
            if order
            else dict(
                intent=OrderIntent.close_position(
                    lot["position_id"],
                    OrderIntent.model_validate(lot["opening_intent"]),
                    quantity=Decimal(lot["remaining_quantity"]),
                    order_type="MARKET",
                    decision_time=now,
                    deadline_at=deadline,
                )
            )
        )
        command = OrderCommand(
            account_id=self.settings.account_id,
            environment=self.settings.environment,
            strategy_instance_id=strategy,
            session_token=value["lease"]["token"],
            session_epoch=value["lease"]["epoch"],
            command_id=identity,
            client_order_id=identity,
            operation="cancel" if order else "submit",
            deadline_at=deadline,
            input_id=value["recovery_id"],
            strategy_config_version="oms-owner-recovery-v1",
            **fields,
        )
        value["pending_command"] = command.model_dump(mode="json")
        value["next_action_at"] = (now + timedelta(seconds=5)).isoformat()
        await self.save(strategy, value)
        return command

    def proof(self, data, head):
        if not data:
            return False
        snapshot, broker = data.get("snapshot", {}), data.get("broker_snapshot") or {}
        try:
            age = (utcnow() - datetime.fromisoformat(broker["started_at"])).total_seconds()
            return bool(
                data.get("account_id") == self.settings.account_id
                and data.get("mode") == self.settings.mode
                and data.get("feed_epoch") == self.store.feed_epoch
                and snapshot.get("feed_epoch") == self.store.feed_epoch
                and broker.get("feed_epoch") == self.store.feed_epoch
                and data.get("safe_for_new_risk") is True
                and data.get("oms_sync_fresh") is True
                and data.get("reconciliation_status") == "POSITIONS_MATCHED"
                and snapshot.get("status") == "AVAILABLE"
                and snapshot.get("reconciliation_status") == "POSITIONS_MATCHED"
                and isinstance(snapshot.get("position_lots"), (list, tuple))
                and isinstance(snapshot.get("items"), (list, tuple))
                and data.get("applied_account_seq", -1) >= head
                and snapshot.get("applied_account_seq", -1) >= head
                and snapshot.get("execution_session") == broker.get("source_session")
                and broker.get("source_session")
                and broker.get("complete") is True
                and broker.get("stable") is True
                and broker.get("managed_positions") is not None
                and -1 <= age <= self.max_snapshot_age
            )
        except (KeyError, TypeError, ValueError):
            return False

    async def prepare(self, strategy, data, *, proof_attempted=True):
        """Return one persisted action. All state decisions use one OMS transaction."""
        async with self.store.transaction():
            await self.observe(strategy)
            value = await self.claim(strategy)
            if not self.active(value):
                return None
            if not set(value["instruments"]).issubset(self.allowlist.get(strategy, ())):
                raise ProtocolError("PENDING_RECOVERY_SCOPE_REQUIRED")
            if value["feed_epoch"] != self.store.feed_epoch:
                raise ProtocolError("RECOVERY_FEED_EPOCH_MISMATCH")
            await self.renew(strategy, value)
            pending = value.get("pending_command")
            if pending:
                command = OrderCommand.model_validate(pending)
                row = await self.store.one(
                    "SELECT result,payload FROM oms.inbox WHERE channel='command' AND id=%s",
                    (command.command_id,),
                )
                if not row:
                    return command
                if row["payload"] != pending:
                    raise ProtocolError("RECOVERY_COMMAND_IDENTITY_CONFLICT")
                value["pending_command"] = None
                await self.save(strategy, value)
            orders = [
                order
                for order in await self.orders(strategy)
                if self.scoped(strategy, order.intent)
            ]
            openings = [order for order in orders if not order.intent.close_position_id]
            # Owner-loss liquidation must retire broker-native protection first.
            # REQUESTED is not proof: only terminal, reconciled cancellations release holds.
            working = [
                order
                for order in orders
                if order.status not in TERMINAL
                and (order in openings or order.intent.order_type == "STOP")
            ]
            if working:
                for order in working:
                    if (
                        order.cancel_state != "REQUESTED"
                        and datetime.fromisoformat(value["next_action_at"]) <= utcnow()
                    ):
                        return await self.action(strategy, value, order=order)
                await self.reason(strategy, value, "WAITING_OPEN_CANCELLATION")
                return None
            if any(
                order.consistency != "CONSISTENT" or order.dispatch_certainty == "UNKNOWN"
                for order in orders
            ) or await self.store.meta("blocked"):
                await self.reason(strategy, value, "WAITING_ORDER_RECONCILIATION")
                return None
            head = await self.store.meta("account_seq", 0)
            if not self.proof(data, head):
                if proof_attempted:
                    await self.reason(strategy, value, "WAITING_PORTFOLIO_PROOF")
                return None
            lots = build_lots(order.view() for order in orders)
            owned = {key: lot for key, lot in lots.items() if Decimal(lot["risk_quantity"]) > 0}
            projected = {
                lot["position_id"]: lot
                for lot in data["snapshot"].get("position_lots", [])
                if lot["strategy_instance_id"] == strategy
                and self.scoped(strategy, OrderIntent.model_validate(lot["opening_intent"]))
                and Decimal(lot["risk_quantity"]) > 0
            }
            if set(owned) != set(projected) or any(
                any(
                    owned[key][field] != projected[key][field]
                    for field in (
                        "strategy_instance_id",
                        "opening_intent",
                        "remaining_quantity",
                        "risk_quantity",
                        "state",
                    )
                )
                for key in owned
            ):
                await self.reason(strategy, value, "WAITING_LOT_PROOF")
                return None
            expected, observed = {}, {}
            for lot in owned.values():
                leg = lot["opening_intent"]["legs"][0]
                key = (leg["instrument_id"], leg["instrument_version"])
                quantity = Decimal(lot["remaining_quantity"])
                expected[key] = expected.get(key, Decimal(0)) + (
                    quantity if leg["side"] == "BUY" else -quantity
                )
            for position in data["snapshot"]["items"]:
                if (
                    position["strategy_instance_id"] == strategy
                    and position["instrument_id"] in self.allowlist[strategy]
                ):
                    key = (position["instrument_id"], position["instrument_version"])
                    observed[key] = observed.get(key, Decimal(0)) + Decimal(position["quantity"])
            if {key: quantity for key, quantity in expected.items() if quantity} != {
                key: quantity for key, quantity in observed.items() if quantity
            }:
                await self.reason(strategy, value, "WAITING_POSITION_PROOF")
                return None
            closing = [
                order
                for order in orders
                if order.intent.close_position_id and order.status not in TERMINAL
            ]
            for key, lot in owned.items():
                maximum = FUTURE_POSITION_CAPS.get(
                    lot["opening_intent"]["legs"][0]["underlying"], 1
                )
                if (
                    lot["state"] != "SETTLED"
                    or not 0 < Decimal(lot["remaining_quantity"]) <= maximum
                ):
                    await self.reason(strategy, value, "WAITING_LOT_RECONCILIATION")
                    return None
                if any(order.intent.close_position_id == key for order in closing):
                    continue
                if datetime.fromisoformat(value["next_action_at"]) <= utcnow():
                    return await self.action(strategy, value, lot=lot)
            if owned or closing:
                await self.reason(strategy, value, "WAITING_OWNED_CLOSE")
                return None
            value["state"], value["reason"] = "COMPLETE", "OWNED_LOTS_PROVEN_FLAT"
            value["proof"] = dict(
                feed_epoch=self.store.feed_epoch,
                account_seq=head,
                execution_session=data["snapshot"]["execution_session"],
                as_of=utcnow().isoformat(),
            )
            value["lease"]["expires_at"] = utcnow().isoformat()
            await self.store.db.execute(
                "UPDATE oms.sessions SET data=%s WHERE strategy=%s",
                (Jsonb(value["lease"]), strategy),
            )
            await self.save(strategy, value)
            if self.log:
                self.log("strategy_recovery_completed", strategy=strategy)
            return None

    async def cycle(self):
        for strategy in self.allowlist:
            # Claim/renew first even if the broker/Portfolio source is unavailable.
            command = await self.prepare(strategy, None, proof_attempted=False)
            if command:
                await self.service.command(command, strategy)
                continue
            async with self.store.transaction():
                if not self.active(await self.current(strategy)):
                    continue
            if self.snapshot is None:
                continue
            try:
                data = await self.snapshot()
            except (ValueError, TimeoutError, OSError, NatsError):
                data = None
            command = await self.prepare(strategy, data)
            if command:
                await self.service.command(command, strategy)

    async def run(self):
        async with self.store.transaction():
            rows = await self.store.all("SELECT strategy,data FROM oms.strategy_recoveries")
            if any(
                self.active(row["data"])
                and not set(row["data"]["instruments"]).issubset(
                    self.allowlist.get(row["strategy"], ())
                )
                for row in rows
            ):
                raise ProtocolError("PENDING_RECOVERY_SCOPE_REQUIRED")
        while True:
            await self.cycle()
            await asyncio.sleep(0.5)
