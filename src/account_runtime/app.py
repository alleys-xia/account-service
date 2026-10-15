"""One supervised account process; a core module failure closes every sibling module."""

import argparse
import asyncio
import json
import signal
from urllib.parse import urlparse

from oms_protocol.messages import ProtocolError


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
            transport = transport_type(service_type(settings, store))
            transports.append(transport)
            await transport.start()
            if any(t.failure.done() for t in transports):
                raise ProtocolError("ACCOUNT_MODULE_START_FAILED")
        print(
            json.dumps(
                dict(
                    event="account_started",
                    modules=["oms", "risk", "portfolio"],
                    mode=oms_settings.mode,
                    trade_ready=False,
                )
            ),
            flush=True,
        )
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
            print(json.dumps(dict(event="account_cleanup_incomplete")), flush=True)


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
        print(
            json.dumps(
                dict(
                    event="account_failed",
                    error=error.code if isinstance(error, ProtocolError) else type(error).__name__,
                )
            ),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
