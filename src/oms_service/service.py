"""Application transactions: identity, admission, decisions and reliable order journal."""

from datetime import datetime, timedelta

from oms_protocol.common import digest, encode, uid, utcnow
from oms_protocol.messages import ExecutionCommand, ProtocolError, RiskRequest
from oms_protocol.orders import OrderUpdate, ViewSnapshot
from psycopg.types.json import Jsonb

from .domain import TERMINAL, Order, apply_execution, intent_hash, validate_replacement


class Service:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def scope(self, message, strategy=None):
        if (
            message.account_id != self.settings.account_id
            or message.environment != self.settings.environment
        ):
            raise ProtocolError("SCOPE_MISMATCH")
        if strategy is not None and (
            strategy not in self.settings.strategies or message.strategy_instance_id != strategy
        ):
            raise ProtocolError("STRATEGY_NOT_AUTHORIZED")

    async def session(self, strategy, token, epoch):
        row = await self.store.one("SELECT data FROM oms.sessions WHERE strategy=%s", (strategy,))
        value = row["data"] if row else None
        if (
            not value
            or value["token"] != token
            or value["epoch"] != epoch
            or datetime.fromisoformat(value["expires_at"]) <= utcnow()
        ):
            raise ProtocolError("SESSION_EXPIRED_OR_FENCED")
        return value

    async def notify(self, order, status=None, reason=None, command_id=None):
        order.revision += 1
        event = OrderUpdate(
            event_id=uid(),
            order_id=order.order_id,
            revision=order.revision,
            command_id=command_id or order.command_id,
            status=status or order.status,
            reason=reason,
            state=order.view(),
            transport="OMS",
        )
        return await self.store.journal(event.model_dump(mode="json"), order.strategy_instance_id)

    async def reject_command(self, command, code, *, remember=True):
        result = dict(
            accepted=False,
            code=code,
            command_id=command.command_id,
            business_hash=command.business_hash(),
        )
        if remember:
            await self.store.remember(
                "command",
                command.command_id,
                command.business_hash(),
                result,
                command.model_dump(mode="json"),
            )
            event = OrderUpdate(
                event_id=uid(),
                order_id="rejected-" + digest(command.command_id)[:24],
                revision=1,
                command_id=command.command_id,
                status="REJECTED",
                reason=code,
                transport="OMS",
            )
            await self.store.journal(event.model_dump(mode="json"), command.strategy_instance_id)
        return result

    async def request_risk(self, order, command, operation):
        revision = (
            order.intent_revision if operation == "submit" else order.last_intent_revision + 1
        )
        order.last_intent_revision = revision
        request_id = uid()
        order.pending = dict(
            operation=operation,
            revision=revision,
            intent=command.intent.model_dump(mode="json"),
            request_id=request_id,
            command_id=command.command_id,
            session_token=command.session_token,
            session_epoch=command.session_epoch,
            stage="RISK_PENDING",
        )
        seq = await self.notify(
            order,
            "RISK_PENDING" if operation == "submit" else "REPLACE_PENDING",
            command_id=command.command_id,
        )
        request = RiskRequest(
            message_id=request_id,
            account_id=order.account_id,
            environment=order.environment,
            order_id=order.order_id,
            strategy_instance_id=order.strategy_instance_id,
            order_revision=revision,
            intent=command.intent,
            intent_hash=intent_hash(command.intent),
            required_account_seq=seq,
            operation=operation,
        )
        data = request.model_dump(mode="json")
        await self.store.db.execute(
            "INSERT INTO oms.risk_requests VALUES (%s,%s,%s)",
            (request_id, order.order_id, Jsonb(data)),
        )
        await self.store.enqueue(
            f"{self.settings.prefix}.cmd.risk.{order.account_id}.check", request_id, data
        )

    async def execution_command(
        self,
        order,
        *,
        operation,
        command_id,
        intent,
        revision,
        valid_until,
        reservation=None,
        session_epoch=None,
    ):
        message = ExecutionCommand(
            command_id=uid(),
            client_command_id=command_id,
            order_id=order.order_id,
            account_id=order.account_id,
            environment=order.environment,
            strategy_instance_id=order.strategy_instance_id,
            order_revision=revision,
            operation=operation,
            intent=intent,
            intent_hash=intent_hash(intent),
            valid_until=valid_until,
            reservation_id=reservation,
            session_epoch=session_epoch or order.session_epoch,
        )
        data = message.model_dump(mode="json")
        await self.store.db.execute(
            "INSERT INTO oms.execution_commands VALUES (%s,%s,%s)",
            (message.command_id, order.order_id, Jsonb(data)),
        )
        await self.store.enqueue(
            f"{self.settings.prefix}.cmd.exec.{order.account_id}.order", message.command_id, data
        )
        return message

    async def command(self, command, strategy):
        async with self.store.transaction():
            try:
                self.scope(command, strategy)
            except ProtocolError as error:
                await self.store.quarantine(
                    "command",
                    command.command_id,
                    error.code,
                    command.model_dump(mode="json"),
                    freeze=False,
                )
                return dict(accepted=False, code=error.code)
            try:
                existing = await self.store.seen(
                    "command", command.command_id, command.business_hash()
                )
            except ProtocolError as error:
                await self.store.quarantine(
                    "command",
                    command.command_id,
                    error.code,
                    command.model_dump(mode="json"),
                    freeze=False,
                )
                return await self.reject_command(command, error.code, remember=False)
            if existing is not None:
                return existing
            try:
                await self.session(strategy, command.session_token, command.session_epoch)
                if command.deadline_at <= utcnow():
                    raise ProtocolError("COMMAND_EXPIRED")
                if command.operation != "cancel" and await self.store.meta("blocked"):
                    raise ProtocolError("ACCOUNT_RECONCILIATION_REQUIRED")
                if command.operation == "submit":
                    previous = await self.store.one(
                        "SELECT id FROM oms.orders WHERE strategy=%s AND client_order_id=%s",
                        (strategy, command.client_order_id),
                    )
                    if previous:
                        raise ProtocolError("CLIENT_ORDER_ID_ALREADY_USED")
                    order = Order(
                        order_id=uid(),
                        client_order_id=command.client_order_id,
                        strategy_instance_id=strategy,
                        account_id=command.account_id,
                        environment=command.environment,
                        intent=command.intent,
                        command_id=command.command_id,
                        session_token=command.session_token,
                        session_epoch=command.session_epoch,
                    )
                    await self.request_risk(order, command, "submit")
                else:
                    order = await self.store.order(command.order_id)
                    if order is None or order.strategy_instance_id != strategy:
                        raise ProtocolError("ORDER_NOT_FOUND")
                    if command.operation == "cancel":
                        if order.status in TERMINAL:
                            await self.notify(order, command_id=command.command_id)
                        elif order.dispatch_certainty == "NOT_SENT":
                            order.status, order.cancel_state, order.pending = (
                                "CANCELLED",
                                "CONFIRMED",
                                None,
                            )
                            await self.notify(order, command_id=command.command_id)
                        elif order.cancel_state == "REQUESTED":
                            await self.notify(
                                order, "PENDING_CANCEL", command_id=command.command_id
                            )
                        else:
                            order.cancel_state = "REQUESTED"
                            await self.execution_command(
                                order,
                                operation="cancel",
                                command_id=command.command_id,
                                intent=order.intent,
                                revision=order.intent_revision,
                                valid_until=command.deadline_at,
                                session_epoch=command.session_epoch,
                            )
                            await self.notify(
                                order, "PENDING_CANCEL", command_id=command.command_id
                            )
                    else:
                        if order.revision != command.expected_revision:
                            raise ProtocolError("REVISION_CONFLICT")
                        if (
                            order.status not in {"SUBMITTED", "PARTIALLY_FILLED"}
                            or order.pending
                            or order.cancel_state == "REQUESTED"
                            or order.consistency != "CONSISTENT"
                        ):
                            raise ProtocolError("ORDER_NOT_REPLACEABLE")
                        validate_replacement(order.intent, command.intent)
                        await self.request_risk(order, command, "replace")
                await self.store.save(order)
                result = dict(
                    accepted=True,
                    order_id=order.order_id,
                    command_id=command.command_id,
                    business_hash=command.business_hash(),
                )
                await self.store.remember(
                    "command",
                    command.command_id,
                    command.business_hash(),
                    result,
                    command.model_dump(mode="json"),
                )
                return result
            except ProtocolError as error:
                return await self.reject_command(command, error.code)

    async def risk(self, decision):
        data = decision.model_dump(mode="json")
        async with self.store.transaction():
            try:
                self.scope(decision)
                if await self.store.seen("risk", decision.event_id, digest(data)) is not None:
                    return
                row = await self.store.one(
                    "SELECT data FROM oms.risk_requests WHERE id=%s", (decision.request_id,)
                )
                if not row:
                    raise ProtocolError("UNKNOWN_RISK_REQUEST")
                request = RiskRequest.model_validate(row["data"])
                if (decision.order_id, decision.order_revision, decision.intent_hash) != (
                    request.order_id,
                    request.order_revision,
                    request.intent_hash,
                ):
                    raise ProtocolError("RISK_APPROVAL_MISMATCH")
                order = await self.store.order(request.order_id)
                pending = order.pending
                active = bool(
                    pending
                    and pending["request_id"] == decision.request_id
                    and pending["stage"] == "RISK_PENDING"
                )
                if not active:
                    dispatched = await self.store.one(
                        "SELECT id FROM oms.execution_commands WHERE order_id=%s AND data->>'order_revision'=%s AND data->>'reservation_id'=%s",
                        (order.order_id, str(request.order_revision), decision.reservation_id),
                    )
                    await self.store.journal(
                        dict(
                            kind="risk_resolution",
                            request_id=decision.request_id,
                            reservation_id=decision.reservation_id,
                            disposition="DISPATCH_PREPARED" if dispatched else "NOT_DISPATCHED",
                            order_id=order.order_id,
                            dispatch_certainty=order.dispatch_certainty,
                        )
                    )
                else:
                    reason = None
                    if not decision.approved:
                        reason = "RISK_REJECTED"
                    elif decision.applied_account_seq < request.required_account_seq:
                        raise ProtocolError("RISK_WATERMARK_BEHIND")
                    elif (
                        decision.approved_quantity != request.intent.quantity
                        or decision.approved_limit != request.intent.limit_amount
                    ):
                        raise ProtocolError("RISK_APPROVED_BOUNDS_MISMATCH")
                    elif min(decision.valid_until, request.intent.deadline_at) <= utcnow():
                        reason = "APPROVAL_EXPIRED"
                    elif (
                        order.cancel_state in {"REQUESTED", "CONFIRMED"} or order.status in TERMINAL
                    ):
                        reason = "ORDER_NO_LONGER_ACTIVE"
                    elif await self.store.meta("blocked"):
                        reason = "ACCOUNT_RECONCILIATION_REQUIRED"
                    else:
                        try:
                            await self.session(
                                order.strategy_instance_id,
                                pending["session_token"],
                                pending["session_epoch"],
                            )
                        except ProtocolError as error:
                            reason = error.code
                    if reason:
                        operation = pending["operation"]
                        command_id = pending["command_id"]
                        order.pending = None
                        if operation == "submit" and order.status not in TERMINAL:
                            order.status = "EXPIRED" if reason == "APPROVAL_EXPIRED" else "REJECTED"
                        await self.notify(
                            order,
                            "REPLACE_REJECTED" if operation == "replace" else order.status,
                            reason,
                            command_id,
                        )
                        await self.store.journal(
                            dict(
                                kind="risk_resolution",
                                request_id=decision.request_id,
                                reservation_id=decision.reservation_id,
                                disposition="NOT_DISPATCHED",
                                reason=reason,
                                order_id=order.order_id,
                            )
                        )
                    else:
                        message = await self.execution_command(
                            order,
                            operation=pending["operation"],
                            command_id=pending["command_id"],
                            intent=request.intent,
                            revision=request.order_revision,
                            valid_until=min(decision.valid_until, request.intent.deadline_at),
                            reservation=decision.reservation_id,
                            session_epoch=pending["session_epoch"],
                        )
                        if pending["operation"] == "submit":
                            order.dispatch_certainty = "PREPARED"
                            order.status = "DISPATCH_PENDING"
                            order.pending = None
                        else:
                            pending["stage"] = "EXECUTION_PENDING"
                            pending["execution_command_id"] = message.command_id
                        await self.notify(
                            order,
                            "ACCEPTED" if request.operation == "submit" else "REPLACE_PENDING",
                            command_id=message.client_command_id,
                        )
                    await self.store.save(order)
                await self.store.remember(
                    "risk", decision.event_id, digest(data), {"processed": True}, data
                )
            except ProtocolError as error:
                await self.store.quarantine("risk", decision.event_id, error.code, data)

    async def expire_waiting(self):
        # Only a risk wait is safe to expire locally. PREPARED/UNKNOWN may have left this process.
        async with self.store.transaction():
            rows = await self.store.all(
                "SELECT data FROM oms.orders WHERE data->'pending'->>'stage'='RISK_PENDING' AND (data->'pending'->'intent'->>'deadline_at')::timestamptz <= now() ORDER BY id LIMIT 100"
            )
            for row in rows:
                order = Order.model_validate(row["data"])
                pending = order.pending
                order.pending = None
                if pending["operation"] == "submit":
                    order.status = "EXPIRED"
                await self.notify(
                    order,
                    "EXPIRED" if pending["operation"] == "submit" else "REPLACE_REJECTED",
                    "RISK_WAIT_EXPIRED",
                    pending["command_id"],
                )
                await self.store.journal(
                    dict(
                        kind="risk_resolution",
                        request_id=pending["request_id"],
                        disposition="NOT_DISPATCHED",
                        reason="RISK_WAIT_EXPIRED",
                        order_id=order.order_id,
                    )
                )
                await self.store.save(order)

    async def execution(self, report):
        data = report.model_dump(mode="json")
        async with self.store.transaction():
            try:
                self.scope(report)
                if await self.store.seen("execution", report.event_id, digest(data)) is not None:
                    return
                row = await self.store.one(
                    "SELECT data FROM oms.execution_commands WHERE id=%s",
                    (report.execution_command_id,),
                )
                if not row:
                    raise ProtocolError("UNKNOWN_EXECUTION_COMMAND")
                command = ExecutionCommand.model_validate(row["data"])
                if (command.order_id, command.order_revision) != (
                    report.order_id,
                    report.order_revision,
                ):
                    raise ProtocolError("EXECUTION_COMMAND_MISMATCH")
                order = await self.store.order(report.order_id)
                if report.execution_id:
                    existing = await self.store.one(
                        "SELECT order_id FROM oms.executions WHERE id=%s", (report.execution_id,)
                    )
                    if existing and existing["order_id"] != order.order_id:
                        raise ProtocolError("EXECUTION_ID_OWNERSHIP_CONFLICT")
                updated, events, status = apply_execution(order, report, command)
                if report.kind == "FILL":
                    await self.store.db.execute(
                        "INSERT INTO oms.executions VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (report.execution_id, order.order_id),
                    )
                for event in events:
                    await self.store.journal(
                        event.model_dump(mode="json"), order.strategy_instance_id
                    )
                if status:
                    await self.notify(
                        updated, status, report.reason or None, command.client_command_id
                    )
                if updated.consistency == "RECONCILIATION_REQUIRED":
                    await self.store.set_meta("blocked", "EXECUTION_RECONCILIATION_REQUIRED")
                await self.store.save(updated)
                await self.store.remember(
                    "execution", report.event_id, digest(data), {"processed": True}, data
                )
            except ProtocolError as error:
                await self.store.quarantine("execution", report.event_id, error.code, data)

    async def rpc(self, request, strategy):
        self.scope(request, strategy)
        async with self.store.transaction():
            if request.operation == "acquire":
                if not request.run_id:
                    raise ProtocolError("RUN_ID_REQUIRED")
                row = await self.store.one(
                    "SELECT data FROM oms.sessions WHERE strategy=%s", (strategy,)
                )
                current = row["data"] if row else None
                active = current and datetime.fromisoformat(current["expires_at"]) > utcnow()
                if active and current["run_id"] != request.run_id:
                    raise ProtocolError("STRATEGY_ALREADY_ACTIVE")
                session = (
                    current
                    if active
                    else dict(
                        run_id=request.run_id,
                        token=uid(),
                        epoch=(current["epoch"] + 1 if current else 1),
                    )
                )
                session["expires_at"] = (
                    utcnow() + timedelta(seconds=self.settings.session_ttl)
                ).isoformat()
                await self.store.db.execute(
                    "INSERT INTO oms.sessions VALUES (%s,%s) ON CONFLICT(strategy) DO UPDATE SET data=excluded.data",
                    (strategy, Jsonb(session)),
                )
                return dict(
                    session=session,
                    feed_epoch=self.store.feed_epoch,
                    mode="SIMULATION",
                    trade_ready=False,
                )
            await self.session(strategy, request.session_token, request.session_epoch)
            if request.operation in {"renew", "release"}:
                session = await self.session(strategy, request.session_token, request.session_epoch)
                session["expires_at"] = (
                    utcnow()
                    + timedelta(
                        seconds=self.settings.session_ttl if request.operation == "renew" else 0
                    )
                ).isoformat()
                await self.store.db.execute(
                    "UPDATE oms.sessions SET data=%s WHERE strategy=%s", (Jsonb(session), strategy)
                )
                return dict(session=session)
            if request.operation == "snapshot":
                rows = await self.store.all(
                    "SELECT data FROM oms.orders WHERE strategy=%s ORDER BY id LIMIT 1001",
                    (strategy,),
                )
                if len(rows) > 1000 or len(encode(rows).encode()) > 524288:
                    raise ProtocolError("SNAPSHOT_CAPACITY_EXCEEDED")
                head = await self.store.one(
                    "SELECT seq FROM oms.strategy_heads WHERE strategy=%s", (strategy,)
                )
                return ViewSnapshot(
                    status="AVAILABLE",
                    items=tuple(Order.model_validate(x["data"]).view() for x in rows),
                    applied_account_seq=await self.store.meta("account_seq", 0),
                    strategy_seq=head["seq"] if head else 0,
                    feed_epoch=self.store.feed_epoch,
                    reconciliation_status="RECONCILIATION_REQUIRED"
                    if await self.store.meta("blocked")
                    else "SIMULATION_ONLY",
                ).model_dump(mode="json")
            if request.operation == "events":
                rows = await self.store.all(
                    "SELECT payload FROM oms.journal WHERE strategy=%s AND strategy_seq>%s ORDER BY strategy_seq LIMIT %s",
                    (strategy, request.after_seq, request.limit),
                )
                head = await self.store.one(
                    "SELECT seq FROM oms.strategy_heads WHERE strategy=%s", (strategy,)
                )
                bounded = []
                size = 0
                for row in rows:
                    size += len(encode(row["payload"]).encode())
                    if size > 524288:
                        break
                    bounded.append(row["payload"])
                return dict(
                    events=bounded,
                    strategy_seq=head["seq"] if head else 0,
                    feed_epoch=self.store.feed_epoch,
                )
            if request.operation == "command":
                row = await self.store.one(
                    "SELECT result,payload FROM oms.inbox WHERE channel='command' AND id=%s",
                    (request.command_id,),
                )
                if not row or row["payload"].get("strategy_instance_id") != strategy:
                    return dict(found=False)
                return dict(found=True, result=row["result"])
            pending = await self.store.one(
                "SELECT count(*) AS n FROM oms.outbox WHERE NOT published"
            )
            return dict(
                mode="SIMULATION",
                trade_ready=False,
                blocked=await self.store.meta("blocked"),
                pending_outbox=pending["n"],
                feed_epoch=self.store.feed_epoch,
            )
