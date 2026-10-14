"""Serial admission, account replay and transactional reservation/decision/outbox writes."""

from datetime import timedelta
from decimal import Decimal

from oms_protocol.common import digest, uid, utcnow
from oms_protocol.messages import ProtocolError, RiskDecision
from psycopg.types.json import Jsonb

from .ledger import TERMINAL, commitment, empty_order, project
from .policies import Assessment, Engine, Usage


class Service:
    def __init__(self, settings, store, *, extra_rules=()):
        self.settings, self.store = settings, store
        self.engine = Engine(settings, extra_rules=extra_rules)

    def scope(self, value):
        if (
            value.environment != self.settings.environment
            or value.account_id != self.settings.account_id
        ):
            raise ProtocolError("SCOPE_MISMATCH")
        if hasattr(value, "mode") and value.mode != self.settings.mode:
            raise ProtocolError("EXECUTION_DOMAIN_MISMATCH")

    async def blocked(self):
        async with self.store.transaction():
            return await self.store.meta("blocked")

    async def cursor(self):
        async with self.store.transaction():
            return await self.store.meta("feed_epoch"), await self.store.meta("account_seq", 0)

    async def bind_head(self, epoch, head, blocked):
        error = None
        async with self.store.transaction():
            current = await self.store.meta("feed_epoch")
            seq = await self.store.meta("account_seq", 0)
            if current and (current != epoch or seq > head):
                error = "FEED_EPOCH_OR_CURSOR_MISMATCH"
                await self.store.set_meta("blocked", error)
            else:
                await self.store.set_meta("feed_epoch", epoch)
                await self.store.set_meta("oms_blocked", blocked)
        if error:
            raise ProtocolError(error)

    async def apply(self, event):
        error = None
        async with self.store.transaction():
            try:
                self.scope(event)
                if event.feed_epoch != await self.store.meta("feed_epoch"):
                    raise ProtocolError("FEED_EPOCH_MISMATCH")
                seq = await self.store.meta("account_seq", 0)
                event_hash = digest(event.model_dump(mode="json"))
                if event.account_seq <= seq:
                    row = await self.store.one(
                        "SELECT hash FROM risk.events WHERE seq=%s", (event.account_seq,)
                    )
                    if not row or row["hash"] != event_hash:
                        raise ProtocolError("ACCOUNT_EVENT_CONFLICT")
                    return
                if event.account_seq != seq + 1:
                    raise ProtocolError("ACCOUNT_EVENT_GAP")
                payload = event.payload
                order_id = payload["order_id"]
                order = await self.store.order(order_id) or empty_order()
                issue = project(order, payload)
                if issue:
                    await self.store.set_meta("blocked", issue)
                await self.store.save_order(order_id, order)
                await self.store.db.execute(
                    "INSERT INTO risk.events VALUES (%s,%s)", (event.account_seq, event_hash)
                )
                await self.store.set_meta("account_seq", event.account_seq)
            except (KeyError, ValueError, TypeError) as exc:
                error = exc.code if isinstance(exc, ProtocolError) else "INVALID_ACCOUNT_EVENT"
                await self.store.set_meta("blocked", error)
        if error:
            raise ProtocolError(error)

    async def usage(self, strategy):
        # Caller holds the same transaction used to admit and reserve; no race across scopes.
        rows = await self.store.all("SELECT data FROM risk.orders")
        totals = [Decimal("0"), Decimal("0")]
        opens = [0, 0]
        unsettled = False
        for row in rows:
            order = row["data"]
            view = order["view"]
            if not view:
                continue
            amount = commitment(order)
            is_open = amount > 0 and view["status"] not in TERMINAL
            totals[0] += amount
            opens[0] += is_open
            if view["strategy_instance_id"] == strategy:
                totals[1] += amount
                opens[1] += is_open
            if view["consistency"] != "CONSISTENT":
                unsettled = True
        counts = await self.store.one(
            "SELECT count(*) AS account, count(*) FILTER (WHERE strategy=%s) AS strategy FROM risk.requests WHERE created_at > now() - interval '1 minute'",
            (strategy,),
        )
        return (
            Usage(totals[0], opens[0], counts["account"]),
            Usage(totals[1], opens[1], counts["strategy"]),
            unsettled,
        )

    async def decide(self, request, *, external_hold=None):
        error = None
        decision = None
        async with self.store.transaction():
            try:
                self.scope(request)
                payload = request.model_dump(mode="json")
                payload_hash = digest(payload)
                existing = await self.store.one(
                    "SELECT hash,decision FROM risk.requests WHERE id=%s", (request.message_id,)
                )
                if existing:
                    if existing["hash"] != payload_hash:
                        raise ProtocolError("IDEMPOTENCY_CONFLICT")
                    return RiskDecision.model_validate(existing["decision"])
                epoch = await self.store.meta("feed_epoch")
                seq = await self.store.meta("account_seq", 0)
                if not epoch or request.feed_epoch != epoch:
                    raise ProtocolError("FEED_EPOCH_MISMATCH")
                if seq < request.required_account_seq:
                    raise ProtocolError("WATERMARK_BEHIND")
                if request.intent_hash != digest(request.intent.model_dump(mode="json")):
                    raise ProtocolError("INTENT_HASH_MISMATCH")
                order = await self.store.order(request.order_id)
                account, strategy, unsettled = await self.usage(request.strategy_instance_id)
                now = utcnow()
                reason = (
                    await self.store.meta("blocked")
                    or await self.store.meta("oms_blocked")
                    or external_hold
                )
                if unsettled:
                    reason = reason or "ACCOUNT_LEGS_UNSETTLED"
                view = order["view"] if order else {}
                if not reason and (
                    not view
                    or view["strategy_instance_id"] != request.strategy_instance_id
                    or view["pending_operation"] != request.operation
                    or view["pending_intent_revision"] != request.order_revision
                    or view.get("pending_request_id", request.message_id) != request.message_id
                    or view["status"] in TERMINAL
                    or view.get("cancel_state") in {"REQUESTED", "CONFIRMED"}
                ):
                    reason = "ORDER_NO_LONGER_RISK_PENDING"
                if not reason and request.operation == "replace":
                    omitted = {"limit_amount", "decision_time", "deadline_at", "quote_references"}
                    old = {k: v for k, v in view["intent"].items() if k not in omitted}
                    new = request.intent.model_dump(mode="json", exclude=omitted)
                    if old != new or commitment(order) == 0:
                        reason = "INVALID_REPLACEMENT"
                pending = await self.store.one(
                    "SELECT count(*) AS n FROM risk.outbox WHERE NOT published"
                )
                if pending["n"] >= self.settings.max_outbox:
                    reason = reason or "RISK_OUTBOX_FULL"
                assessment = (
                    Assessment(False, reason)
                    if reason
                    else self.engine.evaluate(
                        request.intent,
                        request.strategy_instance_id,
                        account,
                        strategy,
                        commitment(order),
                        now,
                    )
                )
                decision = RiskDecision(
                    account_id=self.settings.account_id,
                    environment=self.settings.environment,
                    event_id=uid(),
                    mode=self.settings.mode,
                    request_id=request.message_id,
                    order_id=request.order_id,
                    order_revision=request.order_revision,
                    intent_hash=request.intent_hash,
                    approved=assessment.approved,
                    reservation_id=uid() if assessment.approved else None,
                    approved_quantity=request.intent.quantity if assessment.approved else None,
                    approved_limit=request.intent.limit_amount if assessment.approved else None,
                    valid_until=min(
                        request.intent.deadline_at,
                        now + timedelta(seconds=self.settings.approval_seconds),
                    ),
                    applied_account_seq=seq,
                    reason=assessment.reason,
                )
                if decision.approved:
                    order["reservations"][request.message_id] = dict(
                        reservation_id=decision.reservation_id,
                        revision=request.order_revision,
                        cost=str(assessment.cost),
                        active=True,
                        definitely_not_executed=False,
                        fee_buffer=str(
                            self.settings.fee_buffer_per_unit_per_leg
                            * len(request.intent.legs)
                            * request.intent.quantity
                        ),
                    )
                    await self.store.save_order(request.order_id, order)
                data = decision.model_dump(mode="json")
                audit = dict(
                    policy_hash=self.settings.policy_hash,
                    cost=str(assessment.cost),
                    expiry_max_loss=str(assessment.expiry_max_loss),
                    account_before=str(account.committed),
                    strategy_before=str(strategy.committed),
                )
                await self.store.db.execute(
                    "INSERT INTO risk.requests(id,hash,strategy,payload,decision,audit) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        request.message_id,
                        payload_hash,
                        request.strategy_instance_id,
                        Jsonb(payload),
                        Jsonb(data),
                        Jsonb(audit),
                    ),
                )
                await self.store.db.execute(
                    "INSERT INTO risk.outbox(message_id,subject,payload) VALUES (%s,%s,%s)",
                    (
                        decision.event_id,
                        f"{self.settings.prefix}.evt.risk.{self.settings.account_id}.decision",
                        Jsonb(data),
                    ),
                )
            except ProtocolError as exc:
                error = exc.code
                if error != "WATERMARK_BEHIND":
                    await self.store.set_meta("blocked", error)
        if error:
            raise ProtocolError(error)
        return decision

    async def health(self):
        async with self.store.transaction():
            usage = {}
            for strategy in self.settings.strategies:
                account, current, unsettled = await self.usage(strategy)
                usage[strategy] = dict(
                    committed=str(current.committed), open_orders=current.open_orders
                )
            return dict(
                mode=self.settings.mode,
                trade_ready=False,
                feed_epoch=await self.store.meta("feed_epoch"),
                applied_account_seq=await self.store.meta("account_seq", 0),
                blocked=await self.store.meta("blocked") or await self.store.meta("oms_blocked"),
                legs_unsettled=unsettled,
                account_committed=str(account.committed),
                strategies=usage,
                account_halted=self.settings.account.halted,
                policy_hash=self.settings.policy_hash,
            )
