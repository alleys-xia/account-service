"""Approved product economics in IB quote units; never a contract qualification cache.

CME product specs (2026-11-11): E-nano equity FAQ, Micro Treasury futures,
1-Ounce Gold FAQ, Micro Corn specs, and FX Product Guide. MZC is quoted in
cents/bushel: 500 bushels means USD 5 per quoted cent, not a multiplier of 500.
Month, conId and broker trading identity still require explicit operator catalogs.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

UnderlyingType = Literal["EQUITY", "INDEX", "RATE", "COMMODITY", "CURRENCY"]


@dataclass(frozen=True)
class FutureProduct:
    underlying_type: str
    settlement: str
    currency: str
    multiplier: Decimal
    tick: Decimal
    exchanges: tuple[str, ...]
    ib_symbol: str | None = None


FUTURES = MappingProxyType(
    {
        "HSI": FutureProduct("INDEX", "CASH", "HKD", Decimal("50"), Decimal("1"), ("HKFE",)),
        "MHI": FutureProduct("INDEX", "CASH", "HKD", Decimal("10"), Decimal("1"), ("HKFE",)),
        "MES": FutureProduct("INDEX", "CASH", "USD", Decimal("5"), Decimal("0.25"), ("CME",)),
        "NES": FutureProduct("INDEX", "CASH", "USD", Decimal("0.5"), Decimal("0.5"), ("CME",)),
        "MTN": FutureProduct(
            "RATE", "CASH", "USD", Decimal("100"), Decimal("0.015625"), ("CBOT", "ECBOT")
        ),
        "MNG": FutureProduct(
            "COMMODITY",
            "CASH",
            "USD",
            Decimal("1000"),
            Decimal("0.001"),
            ("NYMEX",),
            ib_symbol="MHNG",
        ),
        "1OZ": FutureProduct("COMMODITY", "CASH", "USD", Decimal("1"), Decimal("0.25"), ("COMEX",)),
        "MZC": FutureProduct(
            "COMMODITY", "CASH", "USD", Decimal("5"), Decimal("0.5"), ("CBOT", "ECBOT")
        ),
        "M6E": FutureProduct(
            "CURRENCY", "PHYSICAL", "USD", Decimal("12500"), Decimal("0.0001"), ("CME",)
        ),
    }
)
RESTRICTED_FUTURES = frozenset({"NES", "MTN", "MNG", "1OZ", "MZC", "M6E"})
MARKET_FUTURES = RESTRICTED_FUTURES | {"MES", "MHI"}
FUTURE_POSITION_CAPS = MappingProxyType(
    {"NES": 1, "MTN": 1, "MNG": 1, "1OZ": 1, "MZC": 2, "M6E": 1}
)


def entry_cutoff_reason(root, cutoff, now):
    if root == "M6E" and cutoff is None:
        return "FUTURE_ENTRY_CUTOFF_REQUIRED"
    if cutoff is not None and now >= cutoff:
        return "FUTURE_ENTRY_CUTOFF_REACHED"
    return None
