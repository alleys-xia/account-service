# 美股开空与自有批次买回

版本：account-service 0.5.0、oms-protocol / oms-client 0.6.0、portfolio-client 0.5.0，配套 Execution 0.6.1。全部服务和策略 SDK 需同步更新固定版本 wheel；无数据库结构迁移。

## 订单契约与归属

开空使用 `OrderIntent(structure="SINGLE", legs=(STOCK SELL OPEN,), net_price_effect="CREDIT")`，整股、USD、LIMIT；`OrderIntent.close_position(position_id, opening, ...)` 自动生成相同合约的 `BUY / CLOSE / DEBIT`。`is_stock_short_open` 是只读属性，不增加线上字段。协议拒绝 STOCK BUY/CREDIT、SELL/DEBIT。裸卖单腿期权仍不支持。

买回必须指定该策略自己的原始 `position_id`。Risk 在同一事务核验归属、真实成交剩余量与其他待成交平仓预占；部分成交、重复请求、重启回放沿用原有订单事实账本。移除开空白名单仍允许已持有空头申请平仓，但归属、数量、数据一致性和其他原有风控检查继续执行。

Portfolio 保留带符号的数量，SELL 开仓为负，BUY 买回实现 `(开仓价 - 买回价) × 数量`，实际手续费单独扣减。外部手工持仓没有可供策略使用的 lot。QQQ 或同一合约的手工增减不被分配给策略，也不因外部净持仓差额直接冻结系统；继续以自有成交投影核对。详见 [手工持仓共存](manual-positions.md)。

## 开空配置默认关闭

在 Risk 配置顶层显式设置策略与 instrument_id 白名单（名称必须与实际运行实例一致）：

```toml
stock_short_allowlist = { "breakout-frozen-paper" = ["TQQQ"] }
```

每个获准股票的已有 `[instruments.<instrument_id>]` 条目增加正数 `stock_short_budget_per_share`。例如 `"400"` 表示每股预占 400 USD，另加费用；该示例仅在卖出限价不高于 200 USD 时允许开仓，不代表 TQQQ 或其他股票的当前报价。不能把未经核验的示例预算当作部署值。

Risk 逐股固定预占该配置金额，而不把 SELL 限价当作最高成交价；开仓要求 `2 × limit_amount <= stock_short_budget_per_share`。评估结果使用 `risk_basis="STOCK_SHORT_NOTIONAL_BUDGET"`、`expiry_max_loss=null`。账户/策略 `max_order_cost` 至少须覆盖 `预算 × 该单股数 + 费用缓冲`，`max_committed` 还须覆盖并存持仓与挂单。所有获准股票必须在目录内；未知策略、未知合约、非股票或缺预算的白名单使配置加载失败。

Execution 同时需要相同策略白名单及每股预算，并在发送前复核两倍 SELL 限价预算条件；两侧目录和预算由运营者协调部署。Execution 0.6.1 不再请求 IB 报价或可借券数量，借券、保证金和权限由 IB 收单时审核。设置见其 `docs/stock-shorts.md`；本服务不把策略传入的价格引用当成券商借券证明。

## 成交、预算越界与恢复

SELL 限价是最低可接受价格，实际成交仍可能高于准备阶段的报价。Risk 将每股占用提高到 `max(原审批每股占用, 2 × 最高有效实际成交价 + 每股费用缓冲)`；该占用同时覆盖剩余挂单和已成交持仓。超出每股预算时持久记录 `STOCK_SHORT_BUDGET_EXCEEDED`，停止新增风险。只有本版本在没有既有结构性故障时产生的预算冻结才带有允许平仓的来源标记；有效自有 lot 仍可申请买回。结构性冻结不能被预算/费用事件覆盖。修改配置不会删除历史预占、自动解除冻结或改写成交。

每股预算是内部风险额度，系数 2 是保守的准入规则，并非券商保证金公式，也不是最大损失。持仓未实现价格变化并未持续计入账户保证金模型；券商仍独立验证保证金、购买力与交易权限。算法计划止损 50 USD 不替代这些额度，也不能保证实际损失不超过 50 USD。借券费用、价格跳空、停牌、强制回补和流动性均可能导致偏差。

## 本轮验证

临时测试先观察 RED，再验证协议、多空方向、真实 PnL/费用、部分平仓预占、并发买回互斥、审批幂等、成交预算越界与恢复后平仓、外部手工持仓隔离，以及原有期权价差和 MHI 多空路径。测试只使用内存 SQL 边界和合成 broker 回报，未发送任何券商订单，未读取私有凭据。最终整体验收与临时测试清理由策略集成者负责。
