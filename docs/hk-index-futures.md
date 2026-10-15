# HK index futures accounting and risk

HSI and MHI use actual expiry contracts, HKD, INDEX/CASH, whole CONTRACTS, and INDEX_POINTS prices. Catalog multipliers are 50 and 10 respectively. Opening orders require at least `min_future_dte = 1` days to expiry using the Hong Kong date; expired contracts cannot close through admission.

Risk requires three explicit HKD settings. Missing any setting rejects with `CURRENCY_POLICY_REQUIRED`; USD settings never substitute for HKD. Example operator allocation (amounts HKD):

```toml
[account_by_currency.HKD]
max_order_cost = "2100000"
max_committed = "3000000"
max_future_quantity = 1

[strategies_by_currency.alpha.HKD]
max_order_cost = "2100000"
max_committed = "3000000"
max_future_quantity = 1

[fee_buffers_by_currency]
HKD = "100"
```

Each Risk futures catalog entry requires `future_contract_budget`, a positive operator allocation per contract: for example MHI `"500000"` or HSI `"2000000"`. Risk reserves this allocation times contracts plus the HKD fee buffer on either side, including a SELL with a low limit. This fixed capital allocation is neither a guaranteed live notional bound, broker margin, nor maximum loss. Audit reports `risk_basis = "FUTURES_CONTRACT_BUDGET"` and `expiry_max_loss = null`. The order price remains an execution limit. IB checks actual margin.

USD and HKD pending and position monetary exposure stay separate. Legacy health amounts are labelled `currency = "USD"`; `per_currency` contains both summaries. Global account and strategy halts, request counts and open-order counts still apply across all currencies. Quantity and monetary limits use the order currency's settings.

Portfolio records futures asset type, currency, multiplier and both long and short realized PnL. Fees reduce realized net PnL. Futures positions report `trade_cash_flow = null` and `settlement_status = "FUTURES_SETTLEMENT_UNTRACKED"`; daily mark-to-market cash settlement is not implemented. Futures fills never generate a stock-style notional cash debit. Legacy USD stock/option ledger output fields are preserved.

Closing requires a lot owned by the requesting strategy, identical instrument metadata and currency, the inverse side and available filled quantity. Short futures may close with BUY. External/manual holdings remain outside strategy lot ownership and cannot authorize a close.

完整目录与额度样例见 `config/risk.hk-futures.example.toml`；Portfolio 使用配套的 `portfolio.hk-futures.example.toml`。与既有股票／期权并用时合并目录，不能直接删除仍有自有持仓的合约。样例额度属于本次模拟验证配置，实际部署应按计划持仓调整。
