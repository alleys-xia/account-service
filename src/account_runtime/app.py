"""One supervised account process; a core module failure closes every sibling module."""

import argparse
import asyncio
import json
import os
import signal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from oms_protocol.common import utcnow
from oms_protocol.messages import ProtocolError

_LOG_EVENTS = {
    "account_started": "账户服务已启动",
    "account_stopped": "账户服务已停止",
    "account_failed": "账户服务失败",
    "account_cleanup_incomplete": "账户服务清理未完成",
    "oms_started": "OMS已启动",
    "oms_stopped": "OMS已停止",
    "oms_failed": "OMS失败",
    "risk_started": "风控已启动",
    "risk_stopped": "风控已停止",
    "risk_failed": "风控失败",
    "portfolio_started": "持仓服务已启动",
    "portfolio_stopped": "持仓服务已停止",
    "portfolio_failed": "持仓服务失败",
    "strategy_recovery_started": "策略失联，开始清理自有委托和持仓",
    "strategy_recovery_waiting": "策略自有持仓清理等待确认",
    "strategy_recovery_completed": "策略自有委托和持仓已确认清理完成",
}


def log_event(event, *, level="INFO", **fields):
    """Keep account identities, balances and credentials out of process logs."""
    timestamp = (
        utcnow().astimezone(ZoneInfo("Asia/Shanghai")).isoformat(sep=" ", timespec="seconds")
    )
    fields = {
        key: value
        for key, value in fields.items()
        if key in {"mode", "modules", "error", "strategy"}
    }
    if os.environ.get("TRADING_LOG_FORMAT") == "json":
        print(json.dumps(dict(timestamp=timestamp, level=level, event=event, **fields)), flush=True)
        return
    names = {"mode": "模式", "modules": "模块", "error": "错误", "strategy": "策略"}
    values = {
        "IB_PAPER": "IB模拟盘",
        "SIMULATION": "本地模拟",
        "WAITING_OPEN_CANCELLATION": "等待开仓撤单确认",
        "WAITING_ORDER_RECONCILIATION": "等待订单对账",
        "WAITING_PORTFOLIO_PROOF": "等待持仓及券商核对",
        "WAITING_LOT_PROOF": "等待自有持仓明细确认",
        "WAITING_POSITION_PROOF": "等待自有持仓数量核对",
        "WAITING_LOT_RECONCILIATION": "自有持仓数量尚未确认",
        "WAITING_OWNED_CLOSE": "等待自有持仓平仓确认",
    }
    parts = []
    for key, value in fields.items():
        value = "/".join(value) if isinstance(value, list) else values.get(str(value), value)
        parts.append(f"{names[key]}={' '.join(str(value).split())}")
    level_name = {"INFO": "信息", "WARNING": "警告", "ERROR": "错误"}[level]
    print(
        f"{timestamp} {level_name} [账户] {_LOG_EVENTS.get(event, event)} {' '.join(parts)}".rstrip(),
        flush=True,
    )


def validate_domains(settings):
    if len({(s.environment, s.account_id, s.mode) for s in settings}) != 1:
        raise ProtocolError("ACCOUNT_MODULE_DOMAIN_MISMATCH")
    if len({s.nats_url for s in settings}) != 1:
        raise ProtocolError("ACCOUNT_MODULE_BUS_MISMATCH")
    if len({frozenset(s.strategies) for s in settings}) != 1:
        raise ProtocolError("ACCOUNT_MODULE_STRATEGIES_MISMATCH")
    _, risk, portfolio = settings

    def catalog(values):
        return {
            name: (v.asset_type, v.version, v.multiplier, v.currency) for name, v in values.items()
        }

    if catalog(risk.instruments) != catalog(portfolio.instruments):
        raise ProtocolError("ACCOUNT_MODULE_CATALOG_MISMATCH")
    databases = []
    for s in settings:
        url = urlparse(s.database_url.get_secret_value())
        databases.append((url.hostname or "localhost", url.port or 5432, url.path))
    if len(set(databases)) != len(databases):
        raise ProtocolError("SEPARATE_MODULE_DATABASES_REQUIRED")


async def run(oms_settings, risk_settings, portfolio_settings, *, stop=None):
    from oms_service.service import Service as Oms
    from oms_service.storage import Store as OmsStore
    from oms_service.transport import Transport as OmsTransport
    from portfolio_service.service import Service as Portfolio
    from portfolio_service.storage import Store as PortfolioStore
    from portfolio_service.transport import Transport as PortfolioTransport
    from risk_service.service import Service as Risk
    from risk_service.storage import Store as RiskStore
    from risk_service.transport import Transport as RiskTransport

    validate_domains([oms_settings, risk_settings, portfolio_settings])
    if stop is None:
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    stores, transports, waiter = [], [], None
    try:
        for settings, store_type, service_type, transport_type in (
            (oms_settings, OmsStore, Oms, OmsTransport),
            (portfolio_settings, PortfolioStore, Portfolio, PortfolioTransport),
            (risk_settings, RiskStore, Risk, RiskTransport),
        ):
            store = store_type(
                settings.database_url.get_secret_value(),
                environment=settings.environment,
                account_id=settings.account_id,
                mode=settings.mode,
            )
            stores.append(store)
            await store.open()
            options = (
                {"recovery_allowlist": risk_settings.paper_signal_only_allowlist}
                if service_type is Oms
                else {}
            )
            transport = transport_type(service_type(settings, store, **options))
            transports.append(transport)
            await transport.start()
            if any(t.failure.done() for t in transports):
                raise ProtocolError("ACCOUNT_MODULE_START_FAILED")
        recovery = transports[0].service.recovery
        portfolio = transports[1]

        async def recovery_snapshot():
            async with portfolio.gate:
                await portfolio.catchup()
                return await portfolio.service.snapshot()

        recovery.snapshot = recovery_snapshot
        recovery.max_snapshot_age = portfolio_settings.max_snapshot_age
        recovery.log = log_event
        transports[0].spawn(recovery.run())
        log_event("account_started", modules=["oms", "risk", "portfolio"], mode=oms_settings.mode)
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            [waiter, *(t.failure for t in transports)], return_when=asyncio.FIRST_COMPLETED
        )
        for transport in transports:
            if transport.failure in done:
                raise transport.failure.result()
    finally:
        if waiter:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        # Stop OMS admission first, then every sibling; attempt all cleanup even on failure.
        results = await asyncio.gather(*(t.close() for t in transports), return_exceptions=True)
        results += await asyncio.gather(*(s.close() for s in stores), return_exceptions=True)
        if any(isinstance(x, BaseException) for x in results):
            log_event("account_cleanup_incomplete", level="WARNING")
        else:
            log_event("account_stopped")


def main():
    from oms_service.config import load_settings as oms_config
    from portfolio_service.config import load_settings as portfolio_config
    from risk_service.config import load_settings as risk_config

    parser = argparse.ArgumentParser(description="OMS + Risk + Portfolio in one account process")
    parser.add_argument("--oms", default="config/oms.example.toml")
    parser.add_argument("--risk", default="config/risk.example.toml")
    parser.add_argument("--portfolio", default="config/portfolio.example.toml")
    args = parser.parse_args()
    try:
        asyncio.run(
            run(oms_config(args.oms), risk_config(args.risk), portfolio_config(args.portfolio))
        )
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log_event(
            "account_failed",
            level="ERROR",
            error=error.code if isinstance(error, ProtocolError) else type(error).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
