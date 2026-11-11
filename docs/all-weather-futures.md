# All-weather futures account and execution contract

Protocol release: `oms-protocol==0.8.0`, `oms-client==0.8.0`,
`portfolio-client==0.7.0`. Account service is `0.7.0`; execution service is
`0.8.0`. Upgrade dependent services together and retain their existing journals.
Optional stop fields are omitted from historical non-stop wire payloads, so
existing intent and nested command hashes remain valid.

## Approved economics

All values below are USD per API quote unit, with explicit contract months.
`price_unit="INDEX_POINTS"` remains the existing protocol name for all futures
quote points; it does not imply that the underlying is an equity index.

| Product | underlying_type | settlement | multiplier | price_increment | Max contracts, including pending |
| --- | --- | --- | ---: | ---: | ---: |
| NES | INDEX | CASH | 0.5 | 0.5 | 1 |
| MTN | RATE | CASH | 100 | 0.015625 | 1 |
| 1OZ | COMMODITY | CASH | 1 | 0.25 | 1 |
| MZC | COMMODITY | CASH | 5 | 0.5 | 2 |
| M6E | CURRENCY | PHYSICAL | 12500 | 0.0001 | 1 |

Sources: CME [E-nano FAQ](https://www.cmegroup.com/articles/faqs/faq-e-nano-equity-index-futures.html),
[Micro Treasury specifications](https://www.cmegroup.com/articles/2024/micro-treasury-futures-vs-yield-futures.html),
[1-ounce gold FAQ](https://www.cmegroup.com/articles/faqs/faq-1-oz-gold-futures.html),
[Micro Corn specifications](https://www.cmegroup.com/markets/agriculture/grains/micro-corn/specs),
and [FX product guide](https://www.cmegroup.com/markets/fx/fx-product-guide).

The shared `oms_protocol.futures.FUTURES` table is an economic allowlist, not
proof of a qualified contract or available market-data permissions. Execution
checks each configured conId, symbol, exchange, trading class, local symbol,
expiry, raw multiplier, tick and price magnifier against fresh IB contractDetails.

## Operator configuration

Use the actual strategy instance `all_weather_trend` in all allowlists and
strategy limits. The product plugin identifier can separately be
`all-weather-trend`. These are different identifiers.

Risk requires `mode="IB_PAPER"`, `require_portfolio=true`, and an explicit
`paper_market_allowlist` for the instrument IDs. New roots are denied for
unlisted strategies for LIMIT, MARKET and STOP alike. Global and strategy
`max_future_quantity=2` permit the approved MZC size, while the independent
per-product caps above still apply across all months and working opening orders.

Example additions to a private Risk configuration (amounts are illustrative
fixed reservations, not validated broker margins):

```toml
[paper_market_allowlist]
all_weather_trend = ["NES2612", "MTN2612", "1OZ2702", "MZC2703", "M6E2612"]

[strategies.all_weather_trend]
halted = true
max_order_cost = "20000"
max_committed = "40000"
max_future_quantity = 2
max_open_orders = 12

[instruments.MZC2703]
asset_type = "FUTURE"
underlying = "MZC"
expiry = 2027-02-19
underlying_type = "COMMODITY"
settlement = "CASH"
currency = "USD"
multiplier = "5"
price_increment = "0.5"
future_contract_budget = "2500"
```

Repeat the instrument record with the table's exact economics and each freshly
qualified expiry. `future_contract_budget` is a mandatory, static USD reservation
per contract; it is not the current notional value, a price cap, a worst-case
loss, or IB initial margin. Risk reserves that amount plus the configured USD
`fee_buffer_per_unit_per_leg`. Strategy sizing must independently enforce the
current notional exposure, broker margin budget and daily loss limits. The
generator must supply operator-approved budgets; contractDetails does not
establish them. Keep `halted=true` during observe deployment. Existing owned
closes remain permitted subject to ownership, available quantity and reconciliation.

Portfolio can use `catalog_file` pointing at the Risk configuration: its loader
retains `asset_type`, `version`, `currency`, `multiplier`, and `underlying`.
New futures must retain the explicit product root; otherwise economic ledger
recording fails closed. Never replace a catalog entry for an outstanding lot.

Execution uses the same canonical economics plus `symbol`, `ib_con_id`,
`exchange`, `trading_class` and `local_symbol`. Each new root requires a nonempty
local symbol; the symbol and trading class must equal its approved root.
Supply `ib_timezone` as the actual IB Gateway login timezone. Credentials stay
in `EXECUTION_IB_ACCOUNT`, `EXECUTION_NATS_PASSWORD`, and corresponding account
service environment variables.

MZC contract qualification observed on 2026-11-11 returned raw multiplier 500,
raw minTick 0.005, and priceMagnifier 100. Its execution catalog therefore adds:

```toml
[instruments.MZC2703]
asset_type = "FUTURE"
underlying = "MZC"
symbol = "MZC"
ib_con_id = 833200849
exchange = "CBOT"
trading_class = "MZC"
local_symbol = "MZCH7"
expiry = 2027-02-19
underlying_type = "COMMODITY"
settlement = "CASH"
currency = "USD"
multiplier = "5"
price_increment = "0.5"
ib_multiplier = "500"
price_magnifier = 100
price_units_verified = false
```

This is an observed contract identity, not authorization to trade it. Requalify
before use. The other new products have magnifier 1; `ib_multiplier` can be
omitted or set equal to their canonical multiplier. MZC must explicitly use
500 and 100. Qualification verifies `raw multiplier / magnifier == multiplier`
and `raw minTick * magnifier == price_increment`; the actual IB Contract keeps
its raw multiplier 500. Prices stay in API cents for orders, stops and fills:
a 450.0-to-450.5 move earns USD 2.50 for one MZC contract. No automatic price
rescaling is applied.

IB's [ContractDetails reference](https://interactivebrokers.github.io/tws-api/classIBApi_1_1ContractDetails.html)
describes the price magnifier. A
[primary integration report with actual CBOT order/fill observations](https://github.com/nautechsystems/nautilus_trader/pull/5322)
confirms API history, orders and fills share cents, while raw minTick and
contract multiplier use dollar units. Own MZC end-to-end unit evidence is still
required before changing `price_units_verified` to true. A quote or historical
request alone does not validate fills. While false, new orders and priced
closes are blocked; cancellation and an owned price-free MARKET close remain
available. Incoming MZC executions are retained in raw audit/quarantine until
the operator verifies the units and replays reconciliation; they are never
silently interpreted into the economic ledger.

For M6E, set `entry_cutoff_at` in Risk and Execution to a verified, timezone-aware
instant strictly before the earliest applicable first-notice, last-trade and
broker restriction, and before the configured expiry date. Missing or reached
cutoff blocks entry. Do not synthesize a cutoff from the expiry code. Owned
closes and cancellation remain available after the cutoff.

## Native protection lifecycle

```python
protection = OrderIntent.close_position(
    position_id,
    opening_intent,
    quantity=remaining_quantity,
    order_type="STOP",
    stop_amount="450.5",
    time_in_force="GTC",
    decision_time=now,
    deadline_at=command_deadline,
)
```

STOP is SINGLE FUTURE CLOSE only, with a positive `stop_amount` and no
`limit_amount`. RiskDecision signs `approved_stop`. Execution sends IB
`orderType="STP"`, `auxPrice=stop_amount`, `tif="GTC"`; the short command
deadline bounds admission, not the lifetime of the broker GTC order.

All three boundaries permit price-only replacement and refuse loosening a
protective stop. Initial protection requires matching broker openOrder terms;
a status-only callback is not proof of the trigger. Replacements require a
fresh matching callback after send. Every later STOP openOrder must match the
confirmed or explicitly pending replacement plan; changed price, quantity or
TIF quarantines the order for reconciliation.

Working and pending-cancel STOPs reserve their remaining close quantity.
An additional market close is rejected until cancellation is confirmed and
its actual fills are reconciled. After a partial stop fill, only the remaining
owned quantity can be closed. OMS owner-loss recovery cancels protective STOPs
before using its existing market-liquidation workflow; it does not silently
transfer an expired lease to a new owner. Checkpoint restoration must honor that
recovery state.
